"""Compare a sealed simulator run with one independently normalized OUCH session.

Reference CSV (UTF-8, comma-delimited, broker wire order):
symbol,token,type,time_ns,price_raw,quantity,reason

A/U/E require price and quantity. J/C require reason. C may include a canceled
quantity derived by the broker; J/C leave price blank and J leaves quantity blank.
Leave symbol blank only for a bridge-level J with no assigned instrument.
Optional columns: side, previous_token (U), request_time_ns. Use a decimal price
column in place of price_raw with --price-decimals SYMBOL=N for each priced symbol.
time_ns is the exchange response timestamp; request_time_ns is a corresponding
command send timestamp on a comparable clock. Normalize each OUCH session separately.
"""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.ouch import decode_reply
from dashboard.run_audit import file_sha256, verify_run


KINDS = "AJUCE"
PRICED = "AUE"
U64_MAX = 2**64 - 1
I32_MIN, I32_MAX = -(2**31), 2**31 - 1
REQUIRED = {"symbol", "token", "type", "time_ns", "quantity", "reason"}
OPTIONAL = {"side", "previous_token", "request_time_ns"}
DIFFERENCES = ("missing", "extra", "order", "fields")


def symbol_key(value):
    if not isinstance(value, str):
        raise ValueError("invalid equity symbol")
    symbol = value.strip().upper().removesuffix(".E")
    if not re.fullmatch(r"[A-Z0-9]{1,30}", symbol):
        raise ValueError(f"invalid equity symbol: {value!r}")
    return symbol + ".E"


def order_token(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,14}", value):
        raise ValueError(f"invalid {label}")
    return value


def integer(value, minimum, maximum, label):
    if isinstance(value, str) and len(value) <= 21 and re.fullmatch(r"-?[0-9]+", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}: expected integer in {minimum}..{maximum}")
    return value


def optional_int(value, minimum, maximum, label):
    return None if value in ("", None) else integer(value, minimum, maximum, label)


def price_in_raw(value, decimals):
    if len(value) > 60 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("invalid decimal price; use a decimal point and no currency or thousands separator")
    whole, _, fraction = value.partition(".")
    if fraction[decimals:].strip("0"):
        raise ValueError("decimal price cannot be represented exactly with the supplied price decimals")
    scaled = int(whole + fraction[:decimals].ljust(decimals, "0"))
    return integer(scaled, 1, I32_MAX, "price_raw")


@dataclass(frozen=True, slots=True)
class Response:
    row: int
    symbol: str | None
    token: str
    kind: str
    time_ns: int
    price_raw: int | None
    quantity: int | None
    reason: int | None
    side: str | None = None
    previous_token: str | None = None
    request_time_ns: int | None = None
    wire_sequence: int | None = None
    wire_row: int | None = None
    delivery_time_ns: int | None = None


def broker_response(row, line, price_column, decimals):
    token = order_token(row["token"], "token")
    kind = row["type"]
    if kind not in KINDS or len(kind) != 1:
        raise ValueError("type must be A, J, U, C or E")
    symbol = None if not row["symbol"] and kind == "J" else symbol_key(row["symbol"])
    time_ns = integer(row["time_ns"], 0, U64_MAX, "time_ns")
    request_time = optional_int(row.get("request_time_ns"), 0, time_ns, "request_time_ns")
    quantity = optional_int(row["quantity"], 0, U64_MAX, "quantity")
    reason = optional_int(row["reason"], 0 if kind == "C" else I32_MIN,
                          255 if kind == "C" else I32_MAX, "reason")
    price = row[price_column]
    if kind in PRICED:
        if quantity is None or (kind in "AE" and not quantity) or reason is not None:
            raise ValueError(f"{kind} needs a valid quantity and blank reason")
        if price_column == "price" and symbol not in decimals:
            raise ValueError(f"supply --price-decimals {symbol}=N for decimal prices")
        price = (price_in_raw(price, decimals[symbol]) if price_column == "price"
                 else integer(price, 1, I32_MAX, "price_raw"))
    else:
        if price or reason is None or (kind == "J" and quantity is not None):
            raise ValueError(f"{kind} requires a reason and blank price; J also requires blank quantity")
        price = None
    side = row.get("side") or None
    if side not in (None, "B", "S"):
        raise ValueError("side must be B, S or blank")
    previous = row.get("previous_token") or None
    if previous is not None:
        if kind != "U":
            raise ValueError("previous_token applies to U replies")
        previous = order_token(previous, "previous_token")
    return Response(line, symbol, token, kind, time_ns, price, quantity, reason, side, previous, request_time)


def read_broker(path, decimals):
    first_hash = file_sha256(path)
    responses = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        fields = reader.fieldnames or []
        columns = set(fields)
        prices = columns & {"price_raw", "price"}
        if (not REQUIRED <= columns or len(prices) != 1 or len(fields) != len(columns) or
                columns - (REQUIRED | OPTIONAL | prices)):
            raise ValueError("invalid CSV header: use the normalized OUCH schema shown by --help")
        price_column = next(iter(prices))
        for row in reader:
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("wrong number of CSV columns")
                values = {key: value.strip() for key, value in row.items()}
                responses.append(broker_response(values, reader.line_num, price_column, decimals))
            except ValueError as error:
                raise ValueError(f"broker CSV line {reader.line_num}: {error}") from error
    if not responses:
        raise ValueError("broker reference contains no OUCH replies")
    if first_hash != file_sha256(path):
        raise ValueError("broker CSV changed while being read")
    return responses, first_hash


def read_run(directory):
    manifest = verify_run(directory)
    if manifest.get("status") not in ("finished", "complete"):
        raise ValueError("broker reconciliation requires a successfully finished run")
    responses = []
    uses_wire_order = external_wire_order(manifest)
    engine_by_key = {}
    wire_records = []
    sends = {}
    reply_counts = Counter()
    count = 0
    digest = sha256()
    with (directory / "events.ndjson").open("rb") as stream:
        for count, line in enumerate(stream, 1):
            digest.update(line)
            try:
                event = json.loads(line)
                if event["index"] != count:
                    raise ValueError("invalid audit ledger index")
                if uses_wire_order and event["kind"] == "external_reply":
                    wire_records.append((count, event["data"]))
                    continue
                if event["kind"] != "ouch":
                    continue
                data = event["data"]
                symbol = symbol_key(data["symbol"])
                time_ns = integer(data["time_ns"], 0, U64_MAX, "time_ns")
                request = optional_int(data.get("request_sequence"), 1, U64_MAX, "request_sequence")
                if data["direction"] == "send":
                    request_key = (symbol, request)
                    if request is None or request_key in sends:
                        raise ValueError("invalid or duplicate simulated request identity")
                    sends[request_key] = time_ns
                    continue
                if data["direction"] != "receive":
                    raise ValueError("unknown audit OUCH direction")
                reply_counts[symbol] += 1
                if data["ouch_sequence"] != reply_counts[symbol]:
                    raise ValueError("invalid audit OUCH reply sequence")
                frame = bytes.fromhex(data["frame_hex"])
                reply = decode_reply(frame)
                if (reply.kind, reply.token, reply.time_ns) != (data["type"], data["token"], time_ns):
                    raise ValueError("audit reply differs from its OUCH frame")
                request_time = sends.get((symbol, request))
                if request_time is not None and request_time > time_ns:
                    raise ValueError("audit reply timestamp precedes its request")
                quantity = (reply.quantity if reply.kind in PRICED else
                            optional_int(data.get("quantity"), 0, U64_MAX, "canceled quantity")
                            if reply.kind == "C" else None)
                response = Response(count, symbol, reply.token, reply.kind, time_ns,
                                    reply.price if reply.kind in PRICED else None, quantity,
                                    reply.reason if reply.kind in "CJ" else None,
                                    reply.side or data.get("side"), reply.previous_token or None, request_time)
                if uses_wire_order:
                    engine_by_key[symbol, reply_counts[symbol]] = (response, frame)
                else:
                    responses.append(response)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"audit ledger line {count}: {error}") from error
    if digest.hexdigest() != manifest["events_sha256"] or count != manifest["records"]:
        raise ValueError("audit ledger checksum or record count differs")
    if uses_wire_order:
        responses = ordered_wire_replies(wire_records, engine_by_key)
    if not responses:
        raise ValueError("sealed run contains no OUCH replies")
    return manifest, responses


def external_wire_order(manifest):
    scenario = manifest["scenario"]
    strategy = scenario.get("strategy")
    return (scenario.get("external_ledger") == 2 and isinstance(strategy, dict)
            and strategy.get("id") == "external_ouch")


def ordered_wire_replies(wire_records, engine):
    if not wire_records:
        raise ValueError("external OUCH run contains no audited wire replies")
    responses = []
    for ordinal, (line, data) in enumerate(wire_records, 1):
        try:
            if integer(data["sequence"], 1, U64_MAX, "wire sequence") != ordinal:
                raise ValueError(f"expected wire sequence {ordinal}")
            frame = bytes.fromhex(data["frame_hex"])
            delivery = optional_int(data["delivery_time_ns"], 0, U64_MAX, "delivery_time_ns")
            symbol, local = data["symbol"], data["local_ouch_sequence"]
            if symbol is None and local is None:
                reply = decode_reply(frame)
                if reply.kind != "J":
                    raise ValueError("wire reply without an engine identity is only valid for bridge-level J")
                response = Response(line, None, reply.token, reply.kind, reply.time_ns, None, None, reply.reason)
            else:
                if symbol is None or local is None or delivery is None:
                    raise ValueError("linked wire reply requires symbol, local OUCH sequence and delivery time")
                key = (symbol_key(symbol), integer(local, 1, U64_MAX, "local OUCH sequence"))
                linked = engine.get(key)
                if linked is None:
                    raise ValueError(f"wire reply has unknown engine identity {key}")
                response, expected = linked
                if expected != frame:
                    raise ValueError(f"wire reply differs from engine frame for {key}")
            responses.append(replace(response, wire_sequence=ordinal, wire_row=line, delivery_time_ns=delivery))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"external reply audit line {line}: {error}") from error
    return responses


def indexed(responses):
    counters = Counter()
    result = {}
    for response in responses:
        identity = (response.symbol, response.token, response.kind)
        counters[identity] += 1
        result[(*identity, counters[identity])] = response
    return result


def delta_stats(values):
    if not values:
        return {"count": 0}
    absolute = sorted(map(abs, values))

    def percentile(percent):
        return absolute[(len(absolute) * percent + 99) // 100 - 1]

    return {"count": len(values), "min_signed_ns": min(values), "max_signed_ns": max(values),
            "p50_absolute_ns": percentile(50), "p95_absolute_ns": percentile(95),
            "p99_absolute_ns": percentile(99), "max_absolute_ns": absolute[-1]}


def identity_detail(key, simulated=None, reference=None):
    symbol, token, kind, occurrence = key
    detail = {"symbol": symbol, "token": token, "type": kind, "occurrence": occurrence,
              "audit_index": simulated.row if simulated else None,
              "broker_csv_line": reference.row if reference else None}
    if simulated and simulated.wire_sequence is not None:
        detail.update(audit_index=simulated.wire_row, wire_sequence=simulated.wire_sequence,
                      engine_audit_index=simulated.row if simulated.symbol is not None else None,
                      delivery_time_ns=simulated.delivery_time_ns)
    return detail


def compare(simulated, reference, args):
    sim, broker = indexed(simulated), indexed(reference)
    counts = Counter()
    fields_compared = Counter()
    details = {kind: [] for kind in DIFFERENCES}
    responses, latencies = [], []
    previous_reference_line = 0

    def record(kind, detail):
        counts[kind] += 1
        if len(details[kind]) < args.detail_limit:
            details[kind].append(detail)

    for key, expected in broker.items():
        if key not in sim:
            record("missing", identity_detail(key, reference=expected))
    for key, actual in sim.items():
        expected = broker.get(key)
        detail = identity_detail(key, actual, expected)
        if expected is None:
            record("extra", detail)
            continue
        if expected.row < previous_reference_line:
            record("order", {**detail, "previous_broker_csv_line": previous_reference_line})
        previous_reference_line = max(previous_reference_line, expected.row)

        field_differences = {}

        def field(name, sim_value, broker_value, tolerance=0):
            fields_compared[name] += 1
            difference = (abs(sim_value - broker_value) > tolerance
                          if type(sim_value) is int and type(broker_value) is int else sim_value != broker_value)
            if difference:
                field_differences[name] = {"simulated": sim_value, "broker": broker_value,
                                           "tolerance": tolerance}

        if expected.price_raw is not None:
            field("price_raw", actual.price_raw, expected.price_raw, args.price_tolerance_raw)
        if expected.quantity is not None:
            field("quantity", actual.quantity, expected.quantity, args.quantity_tolerance)
        if expected.reason is not None:
            field("reason", actual.reason, expected.reason)
        if expected.side is not None:
            field("side", actual.side, expected.side)
        if expected.previous_token is not None:
            field("previous_token", actual.previous_token, expected.previous_token)

        adjusted = expected.time_ns + args.broker_time_offset_ns
        field("response_time_ns", actual.time_ns, adjusted, args.time_tolerance_ns)
        responses.append(actual.time_ns - adjusted)
        if expected.request_time_ns is not None:
            broker_duration = expected.time_ns - expected.request_time_ns
            duration = actual.time_ns - actual.request_time_ns if actual.request_time_ns is not None else None
            field("request_to_response_ns", duration, broker_duration, args.latency_tolerance_ns)
            if duration is not None:
                latencies.append(duration - broker_duration)
        if field_differences:
            record("fields", {**detail, "differences": field_differences})

    return {"status": "fail" if counts else "pass", "simulated_replies": len(sim), "broker_replies": len(broker),
            "matched_replies": len(responses), "differences": {kind: counts[kind] for kind in DIFFERENCES},
            "fields_compared": dict(fields_compared), "details": details,
            "response_time_delta_ns": delta_stats(responses), "request_to_response_delta_ns": delta_stats(latencies)}


def nonnegative(value):
    try:
        return integer(value, 0, U64_MAX, "nonnegative threshold")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def decimal_option(value):
    symbol, separator, decimals = value.partition("=")
    try:
        if not separator:
            raise ValueError("supply price decimals as SYMBOL=N")
        return symbol_key(symbol), integer(decimals, 0, 9, "price decimals")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def write_report(destination, report, inputs):
    if destination.exists() and any(destination.samefile(path) for path in inputs):
        raise ValueError("report output cannot replace a source file")
    with TemporaryDirectory(dir=destination.parent) as directory:
        staged = Path(directory) / "report.json"
        staged.write_text(report, encoding="utf-8")
        staged.replace(destination)


def main():
    # JSON and audit errors use UTF-8 consistently when launched from Windows shells.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path, help="sealed simulator run directory")
    parser.add_argument("reference", type=Path, help="independent normalized broker CSV")
    parser.add_argument("--price-decimals", type=decimal_option, action="append", default=[], metavar="SYMBOL=N")
    parser.add_argument("--price-tolerance-raw", type=nonnegative, default=0)
    parser.add_argument("--quantity-tolerance", type=nonnegative, default=0)
    parser.add_argument("--time-tolerance-ns", type=nonnegative, default=0)
    parser.add_argument("--latency-tolerance-ns", type=nonnegative,
                        help="defaults to the response timestamp tolerance")
    parser.add_argument("--broker-time-offset-ns", type=int, default=0,
                        help="signed offset added to broker response time for comparison")
    parser.add_argument("--detail-limit", type=nonnegative, default=20, help="maximum examples per discrepancy category")
    parser.add_argument("--output", type=Path, help="write the comparison report as JSON to this local path")
    args = parser.parse_args()
    if args.latency_tolerance_ns is None:
        args.latency_tolerance_ns = args.time_tolerance_ns
    decimals = dict(args.price_decimals)
    if len(decimals) != len(args.price_decimals):
        parser.error("duplicate --price-decimals symbol")
    try:
        manifest, simulated = read_run(args.run)
        reference, digest = read_broker(args.reference, decimals)
        report = compare(simulated, reference, args)
        report.update(schema=1, scope="normalized OUCH responses and comparable submitted command timing",
                      run_id=manifest["id"], events_sha256=manifest["events_sha256"], broker_csv_sha256=digest,
                      scenario_sha256=manifest["scenario_sha256"], results_sha256=manifest["results_sha256"],
                      timestamp_basis="exchange response time",
                      latency_basis="audited command send to exchange response",
                      reply_order_basis="external wire audit" if external_wire_order(manifest) else "engine generation",
                      bridge_rejections=sum(reply.symbol is None for reply in simulated),
                      time_tolerance_ns=args.time_tolerance_ns, latency_tolerance_ns=args.latency_tolerance_ns,
                      price_tolerance_raw=args.price_tolerance_raw, quantity_tolerance=args.quantity_tolerance,
                      broker_time_offset_ns=args.broker_time_offset_ns, maximum_examples_per_category=args.detail_limit)
        serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            write_report(args.output, serialized,
                         [args.run / "manifest.json", args.run / "events.ndjson", args.reference])
            print(f"{report['status'].upper()}: {report['matched_replies']} matched OUCH replies. {args.output}")
        else:
            print(serialized, end="")
        return 0 if report["status"] == "pass" else 1
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        parser.exit(2, f"Invalid reconciliation input: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
