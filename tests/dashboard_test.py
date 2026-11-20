import json
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from replay_test import (PORT, SECOND, add, directory, execute, message, mold,
                         pcap, seconds, system_event, tick_size, udp_frame)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.server import Controller, PlaybackClock, make_server


BOOK = 71536


def phase(name):
    data = message(b"O", 29)
    struct.pack_into("!I20s", data, 5, BOOK, name.ljust(20))
    return data


def at(data, nanos):
    struct.pack_into("!I", data, 1, nanos)
    return data


def capture_frames(symbol=b"ASELS.E", port=PORT):
    epoch = (SECOND + 2) * 1_000_000_000
    events = [seconds(), system_event(b"O"), directory(BOOK, symbol), tick_size(BOOK), phase(b"P_ARA"),
              struct.pack("!cI", b"T", SECOND + 2), phase(b"P_SUREKLI_ISLEM"),
              at(add(BOOK, b"B", 1, 10000, epoch + 2_000_000, 1), 2_000_000),
              at(add(BOOK, b"S", 2, 10100, epoch + 2_100_000, 1), 2_100_000),
              at(add(BOOK, b"B", 6, 9950, epoch + 4_000_000, 1), 4_000_000),
              at(add(BOOK, b"B", 3, 10000, epoch + 5_000_000, 1, 100), 5_000_000),
              at(add(BOOK, b"S", 4, 10100, epoch + 5_000_000, 2, 100), 5_000_000),
              at(execute(BOOK, b"B", 1, 10, match_id=101), 6_000_000),
              at(execute(BOOK, b"S", 2, 10, match_id=102), 6_000_000),
              at(execute(BOOK, b"B", 3, 15, match_id=103), 7_000_000),
              at(execute(BOOK, b"S", 4, 15, match_id=104), 7_000_000),
              at(system_event(b"C"), 8_000_000)]
    return [udp_frame(mold(1, events), port=port), udp_frame(mold(len(events) + 1, count=0xFFFF), port=port)]


def capture():
    return pcap(capture_frames())


class DashboardTest(unittest.TestCase):
    def test_clock_uses_market_duration_without_reordering(self):
        clock = PlaybackClock()
        clock.restart(7_000_000_000, 4_000_000_000)
        self.assertEqual(clock.seconds_until(9_000_000_000, 4_125_000_000), 1.875)
        clock.restart(7_125_000_000, 4_125_000_000, 1000)
        self.assertAlmostEqual(clock.seconds_until(9_000_000_000, 4_125_000_000), 0.001875)
        clock.restart(7_125_000_000, 4_125_000_000, "max")
        self.assertEqual(clock.seconds_until(9_000_000_000, 4_125_000_000), 0)

    def test_http_controls_playback_and_order_sequence(self):
        with tempfile.TemporaryDirectory() as directory_name:
            root = Path(directory_name)
            (root / "sample.pcap").write_bytes(capture())
            controller = Controller(root, EXECUTABLE)
            server = make_server(controller, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(self.base, timeout=2) as response:
                    html = response.read().decode()
                    self.assertIn("Strateji alış fiyatı", html)
                    self.assertIn("data-help=", html)
                with urlopen(self.base + "/app.js", timeout=2) as response:
                    self.assertIn(b"/api/control", response.read())
                with self.assertRaises(HTTPError) as error:
                    self.command("load", file="../private.pcap", host="239.1.2.3", port=PORT)
                self.assertEqual(error.exception.code, 400)
                self.command("load", file="sample.pcap", host="239.1.2.3", port=PORT)
                self.wait_for(lambda s: s["phase"] == "ready")
                self.command("config", config={"quote_interval_ms": 1, "latency_us": 100})
                self.command("start")
                self.wait_for(lambda s: s["market_phase"] == "P_ARA")
                paused = self.command("pause")
                time.sleep(0.05)
                self.assertEqual(self.get_state()["progress"]["sequence"], paused["progress"]["sequence"])
                self.command("advance_open")
                continuous = self.wait_for(lambda s: s["phase"] == "paused" and s["market_phase"] == "P_SUREKLI_ISLEM")
                self.assertEqual(continuous["speed"], 1)
                self.assertEqual(continuous["progress"]["sequence"], 7)
                self.command("start")
                normal = self.wait_for(lambda s: s["phase"] == "finished")
                self.assertEqual(normal["strategy"]["inventory"], 0)
                self.assertEqual(normal["strategy"]["fills"], 2)
                self.assertAlmostEqual(normal["strategy"]["total_pnl"], 0.1)
                for side in ("bids", "asks"):
                    self.assertEqual(normal["book"][side][0]["quantity"], 95)
                    self.assertEqual(normal["reference_book"][side][0]["quantity"], 85)
                audit = normal["matching_audit"]
                self.assertEqual((audit["observed_quantity"], audit["strategy_quantity"],
                                  audit["historical_quantity"], audit["unallocated_quantity"]), (50, 20, 30, 0))
                frames = normal["journal"]
                self.assertEqual([event["side"] for event in frames if event["type"] == "A"][:2], ["B", "S"])
                self.assertEqual([event["side"] for event in frames if event["type"] == "E"], ["B", "S"])
                self.assertEqual([event["ouch_sequence"] for event in frames if event["direction"] == "receive"],
                                 list(range(1, normal["strategy"]["ouch_received"] + 1)))
                for speed in (1000, "max"):
                    self.command("reset")
                    self.command("speed", value=speed)
                    self.command("start")
                    other = self.wait_for(lambda s: s["phase"] == "finished")
                    self.assertEqual(other["strategy"], normal["strategy"])
                    self.assertEqual(other["journal"], frames)
                    self.assertEqual((other["book"], other["reference_book"], other["matching_audit"]),
                                     (normal["book"], normal["reference_book"], audit))
            finally:
                server.shutdown()
                server.server_close()
                controller.close()
                thread.join(timeout=2)

    def test_select_single_and_multiple_equities_across_partitions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "multi.pcap").write_bytes(pcap(
                capture_frames(port=21001) + capture_frames(b"THYAO", 21002)))
            controller = Controller(root, EXECUTABLE)
            server = make_server(controller, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.base = f"http://127.0.0.1:{server.server_port}"
            source = {"file": "multi.pcap", "host": "239.1.2.3", "port": 21001}
            try:
                self.command("list_instruments", **source)
                listings = self.wait_for(lambda state: state["catalog"]["phase"] == "ready")["catalog"]
                self.assertEqual([(item["symbol"], item["port"]) for item in listings["instruments"]],
                                 [("ASELS.E", 21001), ("THYAO.E", 21002)])
                with self.assertRaises(HTTPError) as invalid:
                    self.command("load", symbols=["THYAO.E", "THYAO.E"], **source)
                self.assertEqual(invalid.exception.code, 400)
                self.command("config", config={"quote_interval_ms": 1})
                self.command("speed", value="max")
                self.command("load", symbols=["THYAO.E", "ASELS.E"], **source)
                self.wait_for(lambda state: state["phase"] == "ready")
                self.command("advance_open")
                paused = self.wait_for(lambda state: state["phase"] == "paused")
                self.assertEqual(paused["focus_symbol"], "THYAO.E")
                self.assertEqual(paused["market_phase"], "P_SUREKLI_ISLEM")
                self.assertEqual(paused["progress"]["source_port"], 21002)
                focused = self.command("focus_instrument", symbol="ASELS.E")
                self.assertEqual(focused["phase"], "paused")
                self.assertEqual(focused["progress"]["sequence"], paused["progress"]["sequence"])
                self.command("start")
                combined = self.wait_for(lambda state: state["phase"] == "finished")
                self.assertEqual(combined["progress"]["sequence"], combined["progress"]["events"])
                by_symbol = {item["symbol"]: item for item in combined["instruments"]}
                self.assertEqual(by_symbol["ASELS.E"]["source_port"], 21001)
                self.assertEqual(by_symbol["THYAO.E"]["source_port"], 21002)
                self.assertEqual(combined["strategy"]["fills"], 2)
                focused = self.command("focus_instrument", symbol="THYAO.E")
                self.assertEqual(focused["strategy"]["fills"], 2)
                self.assertEqual(focused["strategy"]["total_pnl"], combined["strategy"]["total_pnl"])
                self.command("load", symbols=["THYAO.E"], **source)
                self.wait_for(lambda state: state["phase"] == "ready")
                self.command("start")
                single = self.wait_for(lambda state: state["phase"] == "finished")
                self.assertEqual(single["instruments"], [by_symbol["THYAO.E"]])
                self.assertEqual(single["progress"]["events"] * 2, combined["progress"]["events"])
                self.assertEqual(single["progress"]["sequence"], single["progress"]["source_sequence"])
            finally:
                server.shutdown()
                server.server_close()
                controller.close()
                thread.join(timeout=2)

    def get_state(self):
        with urlopen(self.base + "/api/state", timeout=2) as response:
            return json.load(response)

    def command(self, action, **options):
        request = Request(self.base + "/api/control", data=json.dumps({"action": action, **options}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while True:
            state = self.get_state()
            if condition(state):
                return state
            if state["phase"] == "error" or time.monotonic() >= deadline:
                self.fail(f"Beklenen replay aşamasına ulaşılamadı: {state}")
            time.sleep(0.01)


if __name__ == "__main__":
    EXECUTABLE = Path(sys.argv.pop(1)).resolve()
    unittest.main()
