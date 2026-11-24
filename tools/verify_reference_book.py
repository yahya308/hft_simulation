"""Verify full reference-book state from normalized, tagged ITCH event files."""

from argparse import ArgumentParser, ArgumentTypeError
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import sys


SIDES = ("B", "S")
FIELD_COUNTS = {"S": 1, "R": 3, "L": 3, "O": 1, "A": 6, "D": 2, "E": 4, "C": 7, "Y": 0}


def equity(symbol):
    if not isinstance(symbol, str):
        raise ValueError("invalid equity symbol")
    symbol = symbol.strip().upper()
    ticker = symbol.removesuffix(".E")
    if not ticker or not ticker.isascii() or not ticker.isalnum():
        raise ValueError(f"invalid equity symbol: {symbol}")
    return ticker + ".E"


def port_and_path(value):
    port, separator, path = value.partition("=")
    if not separator or not port.isdigit() or not 1 <= int(port) <= 65535 or not path:
        raise ArgumentTypeError("supply each source as UDP_PORT=PATH")
    return int(port), Path(path)


@dataclass(frozen=True, slots=True)
class Order:
    price: int
    remaining: int
    rank: tuple[int, int, int]


@dataclass(slots=True)
class Reference:
    book_id: int
    symbol: str
    decimals: int
    lot: int
    phase: str = ""
    ticks: dict = field(default_factory=dict)
    sides: dict = field(default_factory=lambda: {"B": {}, "S": {}})

    def apply(self, kind, sequence, fields):
        if kind == "R":
            symbol, self.decimals, self.lot = fields
            if (equity(symbol) != self.symbol or type(self.decimals) is not int or
                    not 0 <= self.decimals <= 9 or type(self.lot) is not int or self.lot <= 0):
                raise ValueError("invalid directory update")
            return
        if kind == "L":
            lower, upper, step = fields
            if any(type(value) is not int for value in fields) or step <= 0:
                raise ValueError("invalid tick size")
            self.ticks[lower, upper] = step
            return
        if kind == "O":
            if not isinstance(fields[0], str):
                raise ValueError("invalid book phase")
            self.phase = fields[0].strip().upper()
            return
        if kind == "Y":
            for orders in self.sides.values():
                orders.clear()
            return

        order_id, side, *values = fields
        if type(order_id) is not int or order_id < 0 or side not in SIDES:
            raise ValueError("invalid order identity or side")
        orders = self.sides[side]
        if kind == "A":
            price, quantity, rank_ns, rank_sequence = values
            if (order_id in orders or any(type(value) is not int for value in values) or
                    min(quantity, rank_ns, rank_sequence) < 0):
                raise ValueError(f"duplicate active order or invalid quantity: {order_id}")
            orders[order_id] = Order(price, quantity, (rank_ns, rank_sequence, sequence))
            return
        if order_id not in orders:
            raise ValueError(f"unknown order for {kind}: {order_id}")
        if kind == "D":
            del orders[order_id]
            return
        previous = orders[order_id]
        executed = values[0]
        if type(executed) is not int or not 0 <= executed <= previous.remaining:
            raise ValueError(f"execution exceeds outstanding quantity: {order_id}")
        remaining = previous.remaining - executed
        if remaining:
            orders[order_id] = Order(previous.price, remaining, previous.rank)
        else:
            del orders[order_id]

    def visible(self, side):
        levels = defaultdict(list)
        for order_id, order in self.sides[side].items():
            if order.price > 0 and order.remaining > 0:
                levels[order.price].append((order_id, order.remaining))
        visible = []
        for price in sorted(levels, reverse=side == "B"):
            orders = levels[price]
            visible.append((price, sum(quantity for _, quantity in orders),
                            tuple(sorted(order_id for order_id, _ in orders))))
        return visible


@dataclass(slots=True)
class Verification:
    reference: Reference
    simulation: object
    records: int = 0
    checks: list = field(default_factory=list)

    def apply(self, kind, sequence, time_ns, fields):
        if kind != "S":
            self.reference.apply(kind, sequence, fields)
        sim_fields = [self.reference.book_id, *fields] if kind == "R" else fields
        self.simulation.apply([kind, sequence, time_ns, *sim_fields])
        self.records += 1

    def checkpoint(self, sequence):
        if self.checks and self.checks[-1]["sequence"] == sequence:
            return
        ref, sim = self.reference, self.simulation
        if ((sim.book_id, equity(sim.symbol), sim.price_decimals, sim.round_lot, sim.phase, sim._ticks)
                != (ref.book_id, ref.symbol, ref.decimals, ref.lot, ref.phase, ref.ticks)):
            raise ValueError(f"instrument metadata differs for {ref.symbol} at sequence {sequence}")

        digest = sha256()
        sides = {}
        for side in SIDES:
            raw_orders = ref.sides[side]
            visible = ref.visible(side)
            book = sim._reference_book[side]
            actual_orders = {key: Order(order.price, order.quantity, order.rank)
                             for key, order in book.orders.items()}
            if raw_orders != actual_orders:
                raise ValueError(f"active orders differ for {ref.symbol} {side} at sequence {sequence}")
            price_order = (key * book.sign for key in book.ladder)
            actual_levels = [(price, book.levels[price].quantity, tuple(sorted(book.levels[price].orders)))
                             for price in price_order]
            if visible != actual_levels or len(visible) != len(book.levels):
                raise ValueError(f"displayed depth differs for {ref.symbol} {side} at sequence {sequence}")
            for order_id, order in sorted(raw_orders.items()):
                row = (f"{side}\t{order_id}\t{order.price}\t{order.remaining}\t"
                       f"{order.rank[0]}\t{order.rank[1]}\t{order.rank[2]}\n")
                digest.update(row.encode("ascii"))
            sides[side] = {"active_orders": len(raw_orders), "visible_levels": len(visible),
                           "visible_quantity": sum(quantity for _, quantity, _ in visible)}
        directory = [ref.book_id, ref.symbol, ref.decimals, ref.lot, ref.phase,
                     [(lower, upper, ref.ticks[lower, upper]) for lower, upper in sorted(ref.ticks)]]
        digest.update(json.dumps(directory, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        self.checks.append({"sequence": sequence, "records": self.records, "sides": sides,
                            "sha256": digest.hexdigest()})


def verify_channel(port, path, wanted, each, simulation_type):
    books = {}
    catalog = {}
    started = closed = False
    previous_sequence = first_sequence = records = 0
    with path.open(encoding="utf-8") as lines:
        for line_number, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, list) or len(row) < 4:
                    raise ValueError("invalid normalized ITCH row")
                kind, sequence, timestamp, book_id, *fields = row
                if FIELD_COUNTS.get(kind) != len(fields):
                    raise ValueError("unsupported event or invalid tagged row width")
                if type(sequence) is not int or sequence <= previous_sequence or closed:
                    raise ValueError("non-increasing Mold sequence or event after session close")
                if type(timestamp) is not int or timestamp < 0:
                    raise ValueError("invalid exchange timestamp")
                if not records:
                    first_sequence = sequence
                previous_sequence = sequence
                records += 1
                if kind == "S":
                    if book_id is not None or fields != (["C"] if started else ["O"]):
                        raise ValueError("invalid system event in tagged tape")
                    if started:
                        closed = True
                        for book in books.values():
                            book.apply(kind, sequence, timestamp, fields)
                    started = True
                    continue
                if not started or type(book_id) is not int or not 0 <= book_id < 2**32:
                    raise ValueError("missing tagged session start or invalid book ID")
                if kind == "R":
                    raw_symbol, decimals, lot = fields
                    symbol = equity(raw_symbol)
                    if catalog.setdefault(book_id, symbol) != symbol:
                        raise ValueError("book ID changed instrument")
                    if book_id not in books and (not wanted or symbol in wanted):
                        if any(book.reference.symbol == symbol for book in books.values()):
                            raise ValueError(f"instrument is assigned to multiple books: {symbol}")
                        books[book_id] = Verification(Reference(book_id, symbol, decimals, lot),
                                                      simulation_type({"enabled": False}, symbol=symbol))
                if book_id not in catalog:
                    raise ValueError("book has no directory")
                if (book := books.get(book_id)) is not None:
                    book.apply(kind, sequence, timestamp, fields)
                    if kind in ("O", "Y") or book.records % each == 0:
                        book.checkpoint(sequence)
            except (TypeError, ValueError, IndexError, KeyError) as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
    if not started:
        raise ValueError(f"{path}: no tagged ITCH session start")
    for book in books.values():
        book.checkpoint(previous_sequence)
    return {"udp_port": port, "first_sequence": first_sequence, "last_sequence": previous_sequence,
            "tagged_records": records, "has_session_end": closed,
            "symbols": {book.reference.symbol: book.checks for book in books.values()}}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=port_and_path, metavar="UDP_PORT=PATH")
    parser.add_argument("--symbols", help="comma-separated equity symbols; defaults to all tagged equities")
    parser.add_argument("--checkpoint-every", type=int, default=100_000, metavar="RECORDS")
    parser.add_argument("--output", type=Path, help="write the checkpoint report as JSON")
    parser.add_argument("--expected", type=Path, help="compare with an external checkpoint report of the same schema")
    args = parser.parse_args()
    ports = [port for port, _ in args.sources]
    if len(ports) != len(set(ports)) or args.checkpoint_every <= 0:
        parser.error("UDP ports must be distinct and the checkpoint interval must be positive")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dashboard.simulation import Simulation

    try:
        wanted = {equity(symbol) for symbol in args.symbols.split(",")} if args.symbols else set()
        channels = [verify_channel(port, path, wanted, args.checkpoint_every, Simulation)
                    for port, path in sorted(args.sources)]
        symbols = [symbol for channel in channels for symbol in channel["symbols"]]
        if len(symbols) != len(set(symbols)) or not symbols or (wanted and wanted != set(symbols)):
            raise ValueError("requested instruments are missing or duplicated across feeds")
        report = {"schema": 1, "checkpoint_every": args.checkpoint_every, "channels": channels}
        if args.expected and json.loads(args.expected.read_text(encoding="utf-8")) != report:
            raise ValueError("external checkpoint report differs")
        if args.output:
            inputs = [path for _, path in args.sources]
            if args.expected:
                inputs.append(args.expected)
            if args.output.exists() and any(args.output.samefile(path) for path in inputs):
                raise ValueError("report output cannot replace an input file")
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        checks = sum(len(checkpoints) for channel in channels for checkpoints in channel["symbols"].values())
        records = sum(channel["tagged_records"] for channel in channels)
        print(f"PASS: {records} normalized ITCH records, {len(symbols)} instrument(s), {checks} checkpoints. "
              "Compared raw order state and displayed depth with the simulator's reference book.")
        for channel in channels:
            print(f"UDP {channel['udp_port']}: sequences {channel['first_sequence']}..{channel['last_sequence']}, "
                  f"coverage {'session end' if channel['has_session_end'] else 'capture prefix'}.")
        print("Scope: normalized ITCH book calculations. Wire decoding and broker fills need independent inputs.")
    except (OSError, ValueError) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
