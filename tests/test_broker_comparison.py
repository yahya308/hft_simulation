import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dashboard.ouch import Reply, encode_reply
from dashboard.run_audit import RunAudit


TOOL = Path(__file__).resolve().parents[1] / "tools" / "compare_broker_run.py"
FIELDS = ["symbol", "token", "type", "time_ns", "price_raw", "quantity", "reason",
          "side", "previous_token", "request_time_ns"]


class BrokerComparisonTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / "broker.csv"
        self.audit = RunAudit(self.root / "runs", {"name": "fixture"})
        self.received = {}

    def establish_external_run(self):
        self.audit.finish("aborted", {})
        self.audit = RunAudit(self.root / "runs", {"external_ledger": 2, "strategy": {"id": "external_ouch"}})

    def send(self, kind, token, time_ns, request, *, symbol="ASELS.E"):
        self.audit.record("ouch", {"direction": "send", "symbol": symbol, "type": kind,
                                  "time_ns": str(time_ns), "request_sequence": request, "token": token})

    def reply(self, kind, token, time_ns, *, price=0, quantity=0, reason=0, request=None, previous="",
              symbol="ASELS.E"):
        ordinal = self.received.get(symbol, 0) + 1
        self.received[symbol] = ordinal
        frame = encode_reply(Reply(kind, time_ns, token, 125, "B", 27, price, quantity, previous, reason))
        self.audit.record("ouch", {"direction": "receive", "symbol": symbol, "type": kind, "token": token,
                                  "time_ns": str(time_ns), "request_sequence": request, "side": "B",
                                  "ouch_sequence": ordinal, "frame_hex": frame.hex(),
                                  "quantity": quantity or None})
        return frame

    def wire(self, ordinal, frame, symbol=None, local=None, delivery=None):
        self.audit.record("external_reply", {"sequence": ordinal, "frame_hex": frame.hex(), "symbol": symbol,
                                            "local_ouch_sequence": local,
                                            "delivery_time_ns": str(delivery) if delivery is not None else None})

    def scenario(self):
        self.send("O", "START", 100, 1)
        self.reply("A", "START", 130, price=123_000, quantity=10, request=1)
        self.reply("E", "START", 150, price=123_000, quantity=3)
        self.reply("E", "START", 170, price=123_000, quantity=2)
        self.send("U", "AMEND", 185, 2)
        self.reply("U", "AMEND", 210, price=123_500, quantity=4, request=2, previous="START")
        self.send("X", "START", 220, 3)
        self.reply("C", "AMEND", 240, quantity=4, request=3, reason=1)
        self.reply("J", "REJECT", 250, request=4, reason=-2)
        self.audit.finish("finished", {"fills": 2})

        rows = [
            ("START", "A", 130, 123_000, 10, "", "", 100),
            ("START", "E", 150, 123_000, 3, "", "", ""),
            ("START", "E", 170, 123_000, 2, "", "", ""),
            ("AMEND", "U", 210, 123_500, 4, "", "START", 185),
            ("AMEND", "C", 240, "", 4, 1, "", 220),
            ("REJECT", "J", 250, "", "", -2, "", ""),
        ]
        return [["ASELS", token, kind, time, price, qty, reason, "B", previous, request]
                for token, kind, time, price, qty, reason, previous, request in rows]

    def write_csv(self, rows, fields=FIELDS):
        with self.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)

    def compare(self, *args):
        return subprocess.run([sys.executable, "-B", str(TOOL), str(self.audit.directory), str(self.csv), *args],
                              capture_output=True, encoding="utf-8", check=False)

    def test_orders_and_partial_fills_match_by_identity_and_occurrence(self):
        self.write_csv(self.scenario())
        run = self.compare()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["matched_replies"], 6)
        self.assertEqual(result["differences"], dict(missing=0, extra=0, order=0, fields=0))
        self.assertEqual(result["response_time_delta_ns"]["count"], 6)
        self.assertEqual(result["request_to_response_delta_ns"]["count"], 3)
        self.assertEqual(result["request_to_response_delta_ns"]["max_absolute_ns"], 0)

    def test_offset_and_tolerances_apply_without_masking_timing_diagnostics(self):
        rows = self.scenario()
        for row in rows:
            row[3] -= 10
            if row[9] != "":
                row[9] -= 10
        rows[0][9] -= 2
        rows[1][3] += 1
        rows[2][4] += 1
        rows[2][5] -= 1
        self.write_csv(rows)
        strict = self.compare("--broker-time-offset-ns", "10")
        self.assertEqual(strict.returncode, 1, strict.stderr)
        self.assertEqual(json.loads(strict.stdout)["differences"]["fields"], 3)

        report = self.root / "report.json"
        tolerant = self.compare("--broker-time-offset-ns", "10", "--latency-tolerance-ns", "2",
                                "--time-tolerance-ns", "1", "--price-tolerance-raw", "1",
                                "--quantity-tolerance", "1", "--output", str(report))
        self.assertEqual(tolerant.returncode, 0, tolerant.stderr)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((result["response_time_delta_ns"]["min_signed_ns"],
                          result["request_to_response_delta_ns"]["max_absolute_ns"]), (-1, 2))

    def test_exposes_missing_extra_wrong_order_and_differing_fields(self):
        rows = self.scenario()
        rows[1][5] += 1
        rows[4][6] = 9
        missing = ["ASELS", "OTHER", "J", 260, "", "", -2, "", "", ""]
        self.write_csv([rows[1], rows[0], *rows[3:], missing])
        run = self.compare()
        self.assertEqual(run.returncode, 1, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["differences"], dict(missing=1, extra=1, order=1, fields=2))
        self.assertEqual(report["details"]["extra"][0]["occurrence"], 2)
        self.assertEqual(report["details"]["missing"][0]["token"], "OTHER")
        self.assertEqual(report["details"]["fields"][1]["differences"]["reason"]["broker"], 9)

    def test_decimal_scale_and_wire_unchanged_zero_replace_quantity(self):
        self.send("U", "REPLACE", 100, 1)
        self.reply("U", "REPLACE", 150, price=123_500, quantity=0, previous="OLD", request=1)
        self.audit.finish("finished", {})
        rows = [["ASELS.E", "REPLACE", "U", 150, "123.5000", 0, "", "B", "OLD", ""]]
        decimal_fields = ["price" if field == "price_raw" else field for field in FIELDS]
        self.write_csv(rows, decimal_fields)
        no_scale = self.compare()
        self.assertEqual(no_scale.returncode, 2)
        run = self.compare("--price-decimals", "ASELS=3")
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["request_to_response_delta_ns"], {"count": 0})
        self.assertEqual(result["fields_compared"]["quantity"], 1)

    def test_reference_latency_needs_an_audited_simulator_send(self):
        rows = self.scenario()
        rows[-1][9] = 240
        self.write_csv(rows)
        run = self.compare()
        self.assertEqual(run.returncode, 1, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["fields_compared"]["request_to_response_ns"], 4)
        self.assertEqual(report["request_to_response_delta_ns"]["count"], 3)
        self.assertEqual(report["details"]["fields"][0]["differences"]["request_to_response_ns"],
                         {"simulated": None, "broker": 10, "tolerance": 0})

    def test_external_v2_compares_global_wire_order_and_keeps_engine_time_context(self):
        self.establish_external_run()
        self.send("O", "FIRST", 100, 1)
        first = self.reply("A", "FIRST", 130, price=123_000, quantity=10, request=1)
        self.send("O", "SECOND", 105, 1, symbol="THYAO.E")
        second = self.reply("A", "SECOND", 135, price=421_000, quantity=20, request=1, symbol="THYAO.E")
        self.wire(1, second, "THYAO.E", 1, 145)
        self.wire(2, first, "ASELS.E", 1, 150)
        self.wire(3, encode_reply(Reply("J", 155, "UNKNOWN", reason=-5)))
        self.audit.finish("finished", {})

        rows = [["THYAO.E", "SECOND", "A", 135, 421_000, 20, "", "B", "", 105],
                ["ASELS.E", "FIRST", "A", 130, 123_000, 10, "", "B", "", 100],
                ["", "UNKNOWN", "J", 155, "", "", -5, "", "", ""]]
        self.write_csv(rows)
        run = self.compare()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["reply_order_basis"], "external wire audit")
        self.assertEqual((report["matched_replies"], report["bridge_rejections"],
                          report["request_to_response_delta_ns"]["count"]), (3, 1, 2))

        self.write_csv([rows[1], rows[0], rows[2]])
        wrong = self.compare()
        self.assertEqual(wrong.returncode, 1, wrong.stderr)
        report = json.loads(wrong.stdout)
        self.assertEqual(report["differences"]["order"], 1)
        self.assertEqual(report["details"]["order"][0]["wire_sequence"], 2)

    def test_external_v2_rejects_unlinked_or_different_wire_frames(self):
        self.establish_external_run()
        self.reply("A", "FIRST", 130, price=123_000, quantity=10)
        different = encode_reply(Reply("A", 130, "OTHER", 125, "B", 27, 123_000, 10))
        self.wire(1, different, "ASELS.E", 1, 140)
        self.audit.finish("finished", {})
        self.write_csv([["ASELS.E", "FIRST", "A", 130, 123_000, 10, "", "B", "", ""]])
        result = self.compare()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("wire reply differs from engine frame", result.stderr)

    def test_rejects_bad_reference_and_tampered_audit(self):
        original = self.scenario()
        bad = [list(row) for row in original]
        bad[-1][6] = ""
        self.write_csv(bad)
        run = self.compare()
        self.assertEqual(run.returncode, 2)
        self.assertIn("broker CSV line 7", run.stderr)
        self.write_csv(original)
        content = self.csv.read_bytes()
        run = self.compare("--output", str(self.csv))
        self.assertEqual(run.returncode, 2)
        self.assertEqual(content, self.csv.read_bytes())
        with (self.audit.directory / "events.ndjson").open("ab") as stream:
            stream.write(b"{}\n")
        run = self.compare()
        self.assertEqual(run.returncode, 2)
        self.assertIn("SHA-256", run.stderr)


if __name__ == "__main__":
    unittest.main()
