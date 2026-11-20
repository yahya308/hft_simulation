import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from replay_test import (PORT, SECOND, add, delete, directory, execute, flush,
                         mold, pcap, seconds, state, system_event, tick_size, trade, udp_frame)


ASELS = 71_536
OTHER = 73_000
EPOCH_NS = SECOND * 1_000_000_000


class FeedExtractionTest(unittest.TestCase):
    def extract(self, data, *options):
        with tempfile.TemporaryDirectory() as directory_name:
            directory_path = Path(directory_name)
            capture = directory_path / "fixture.pcap"
            output = directory_path / "events.ndjson"
            capture.write_bytes(data)
            process = subprocess.run([EXECUTABLE, capture, "239.1.2.3", str(PORT), output, *options],
                                     check=False, capture_output=True, encoding="utf-8")
            self.assertFalse(output.with_suffix(".ndjson.partial").exists())
            values = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] if output.exists() else None
            return process, values

    def discover(self, data):
        with tempfile.TemporaryDirectory() as directory_name:
            capture = Path(directory_name) / "fixture.pcap"
            capture.write_bytes(data)
            return subprocess.run([EXECUTABLE, capture, "239.1.2.3", str(PORT), "--list-symbols"],
                                  check=False, capture_output=True, encoding="utf-8")

    def test_selects_asels_and_preserves_sequences_and_exchange_time(self):
        opening = [seconds(), system_event(b"O"), directory(OTHER, b"GARAN.E"), directory(ASELS, b"ASELS.E"),
                   tick_size(OTHER), tick_size(ASELS), state(ASELS)]
        own = add(ASELS, b"B", 15, 1100000, EPOCH_NS + 11, 7, quantity=10)
        struct.pack_into("!I", own, 1, 735)
        new_second = struct.pack("!cI", b"T", SECOND + 1)
        events = [state(ASELS), add(OTHER, b"S", 15, 1000000, 44, 7), own, new_second,
                  execute(ASELS, b"B", 15, 2, match_id=2**64 - 1),
                  execute(ASELS, b"B", 15, 1, 1100000, match_id=124, printable=b"Y"),
                  delete(ASELS, b"B", 15), flush(ASELS), system_event(b"C")]
        data = pcap([udp_frame(mold(1, opening)), udp_frame(mold(1, [b"not-itch"]), port=1),
                     udp_frame(mold(7, events))], nano=True)
        process, extracted = self.extract(data)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("\tlast_sequence=15\tstatus=itch_end", process.stderr)
        self.assertEqual(extracted, [
            ["S", 2, EPOCH_NS + 100, "O"],
            ["R", 4, EPOCH_NS + 100, ASELS, "ASELS.E", 4, 1],
            ["L", 6, EPOCH_NS + 100, 0, 0, 50],
            ["O", 7, EPOCH_NS + 100, "CONTINUOUS"],
            ["A", 9, EPOCH_NS + 735, 15, "B", 1100000, 10, EPOCH_NS + 11, 7],
            ["E", 11, EPOCH_NS + 1_000_000_100, 15, "B", 2, 2**64 - 1],
            ["C", 12, EPOCH_NS + 1_000_000_100, 15, "B", 1, 124, 1100000, "N", "Y"],
            ["D", 13, EPOCH_NS + 1_000_000_100, 15, "B"],
            ["Y", 14, EPOCH_NS + 1_000_000_100],
            ["S", 15, EPOCH_NS + 1_000_000_100, "C"],
        ])

    def test_preserves_priced_metadata_and_omits_nondisplayed_trades(self):
        events = [seconds(), system_event(b"O"), directory(ASELS, b"ASELS.E"),
                  add(ASELS, b"S", 9, 10000, 1, 1), trade(ASELS),
                  execute(ASELS, b"S", 9, 2, -123, match_id=0, at_cross=b"Y"),
                  execute(ASELS, b"S", 9, 2, 10000, at_cross=b"\x00", printable=b"?")]
        process, extracted = self.extract(pcap([udp_frame(mold(1, events))]))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([event[0] for event in extracted], ["S", "R", "A", "C", "C"])
        self.assertEqual(extracted[-2:], [
            ["C", 6, EPOCH_NS + 100, 9, "S", 2, 0, -123, "Y", "N"],
            ["C", 7, EPOCH_NS + 100, 9, "S", 2, 123, 10000, None, None],
        ])

    def test_through_sequence_stops_inside_datagram(self):
        events = [seconds(), system_event(b"O"), directory(ASELS, b"ASELS.E"),
                  add(ASELS, b"B", 9, 10000, 1, 1), delete(ASELS, b"B", 9)]
        data = pcap([udp_frame(mold(1, events))])
        process, extracted = self.extract(data, "--through-sequence", "4")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("\tlast_sequence=4\tstatus=sequence_limit", process.stderr)
        self.assertEqual([event[0] for event in extracted], ["S", "R", "A"])
        process, extracted = self.extract(data, "--through-sequence", "6")
        self.assertEqual(process.returncode, 1)
        self.assertIsNone(extracted)
        self.assertIn("capture ended before", process.stderr)

    def test_gap_and_truncation_remove_incomplete_output(self):
        start = udp_frame(mold(1, [seconds(), system_event(b"O"), directory(ASELS, b"ASELS.E")]))
        add_order = [add(ASELS, b"B", 9, 10000, 1, 1)]
        cases = (
            (pcap([start, udp_frame(mold(5, add_order))]), "sequence gap"),
            (pcap([start, udp_frame(mold(4, add_order))])[:-1], "truncated"),
            (pcap([start, udp_frame(mold(4, [add_order[0][:-1]]))]), "sequence 4"),
        )
        for data, problem in cases:
            with self.subTest(problem=problem):
                process, extracted = self.extract(data)
                self.assertEqual(process.returncode, 1)
                self.assertIsNone(extracted)
                self.assertIn(problem, process.stderr)

    def test_missing_and_ambiguous_asels_fail_closed(self):
        for directories, problem in (
                ([directory(OTHER, b"ASELS.R")], "directory was not found"),
                ([directory(ASELS, b"ASELS.E"), directory(OTHER, b"ASELS")], "multiple ASELS")):
            with self.subTest(problem=problem):
                data = pcap([udp_frame(mold(1, [seconds(), system_event(b"O"), *directories]))])
                process, extracted = self.extract(data)
                self.assertEqual(process.returncode, 1)
                self.assertIn(problem, process.stderr)
                self.assertIsNone(extracted)

    def test_tagged_extraction_keeps_two_books_in_wire_sequence(self):
        events = [seconds(), system_event(b"O"), directory(OTHER, b"THYAO.E"),
                  directory(ASELS, b"ASELS.E"), tick_size(OTHER), state(ASELS),
                  add(OTHER, b"S", 9, 10000, 1, 1), add(ASELS, b"S", 9, 11000, 1, 1),
                  execute(OTHER, b"S", 9, 2), execute(ASELS, b"S", 9, 3, 11025, printable=b"Y"),
                  delete(OTHER, b"S", 9), flush(ASELS), system_event(b"C")]
        data = pcap([udp_frame(mold(1, events))])
        process, extracted = self.extract(data, "--through-sequence", "13", "--symbols", "thyao.e, ASELS")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(extracted, [
            ["S", 2, EPOCH_NS + 100, None, "O"],
            ["R", 3, EPOCH_NS + 100, OTHER, "THYAO.E", 4, 1],
            ["R", 4, EPOCH_NS + 100, ASELS, "ASELS.E", 4, 1],
            ["L", 5, EPOCH_NS + 100, OTHER, 0, 0, 50],
            ["O", 6, EPOCH_NS + 100, ASELS, "CONTINUOUS"],
            ["A", 7, EPOCH_NS + 100, OTHER, 9, "S", 10000, 10, 1, 1],
            ["A", 8, EPOCH_NS + 100, ASELS, 9, "S", 11000, 10, 1, 1],
            ["E", 9, EPOCH_NS + 100, OTHER, 9, "S", 2, 123],
            ["C", 10, EPOCH_NS + 100, ASELS, 9, "S", 3, 123, 11025, "N", "Y"],
            ["D", 11, EPOCH_NS + 100, OTHER, 9, "S"],
            ["Y", 12, EPOCH_NS + 100, ASELS],
            ["S", 13, EPOCH_NS + 100, None, "C"],
        ])

    def test_initial_symbol_discovery_excludes_non_equity_and_stops_at_book_state(self):
        events = [seconds(), system_event(b"O"), directory(OTHER, b"THYAO.E"),
                  directory(1, b"WARRANT.E", financial_product=14), directory(2, b"RIGHT.R"),
                  directory(ASELS, b"ASELS.E"), state(OTHER), directory(3, b"INTRADAY.E")]
        opening = udp_frame(mold(1, events))
        delayed = udp_frame(mold(20, [state(OTHER)]))
        process = self.discover(pcap([opening, delayed]))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), [
            {"symbol": "ASELS.E", "book_id": ASELS}, {"symbol": "THYAO.E", "book_id": OTHER},
        ])
        process = self.discover(pcap([udp_frame(mold(1, events[:3])), delayed]))
        self.assertEqual(process.returncode, 1)
        self.assertIn("sequence gap", process.stderr)
        self.assertEqual(process.stdout, "")

    def test_tagged_extraction_fails_if_an_instrument_is_missing_or_duplicated(self):
        opening = [seconds(), system_event(b"O"), directory(ASELS, b"ASELS.E"),
                   directory(OTHER, b"THYAO.E", financial_product=3), state(ASELS)]
        for symbols, error in (("ASELS,THYAO", "THYAO directory was not found"),
                               ("ASELS, asels.e", "duplicate equity symbol")):
            with self.subTest(symbols=symbols):
                process, extracted = self.extract(pcap([udp_frame(mold(1, opening))]), "--symbols", symbols)
                self.assertEqual(process.returncode, 1)
                self.assertIn(error, process.stderr)
                self.assertIsNone(extracted)


if __name__ == "__main__":
    EXECUTABLE = Path(sys.argv.pop(1)).resolve()
    unittest.main()
