import importlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard_test import PORT, capture, capture_frames, pcap
from dashboard.server import Controller, make_server
from dashboard.strategy_registry import Parameter, StrategyDefinition, load_registry


PLUGIN_MODULE = "local_asels_test_plugin"
PLUGIN_ID = "local_probe"
PLUGIN_SOURCE = '''from dashboard.strategy_api import API_VERSION, Enter
from dashboard.strategy_registry import Parameter, StrategyDefinition

instances = []

class Probe:
    def __init__(self, quantity, fail):
        if quantity == 42:
            raise ValueError("Yerel strateji oluşturulamadı")
        self.quantity = quantity
        self.fail = fail
        self.submitted = False
        self.events = []
        instances.append(self)

    def on_event(self, event, view, updates):
        self.events.append(event.sequence)
        if self.fail and view.position:
            raise RuntimeError("Yerel strateji deneme hatası")
        if not self.submitted and view.best_bid is not None and view.best_ask is not None:
            self.submitted = True
            return (Enter("B", view.best_bid, self.quantity),)
        return ()

def get_strategies():
    return (StrategyDefinition(
        "local_probe", "1.2.3", API_VERSION, "Yerel deney", "Bir pasif alış emri verir.",
        {
            "quantity": Parameter("integer", "Adet", "Emir miktarı", 3, minimum=1, maximum=50, unit="pay"),
            "fail": Parameter("boolean", "Hata", "Gerçekleşme sonrası durur", False),
        }, Probe),)
'''


class StrategyRegistryTests(unittest.TestCase):
    def test_duplicate_id_and_incompatible_api_are_rejected(self):
        for strategy_id, api_version in (("two_sided_quote", 1), ("unsupported", 999)):
            with self.subTest(strategy=strategy_id, api_version=api_version):
                module = ModuleType("registry_probe_for_test")
                module.get_strategies = lambda: (StrategyDefinition(
                    strategy_id, "1.0.0", api_version, "Deneme", "Geçersiz seçenek",
                    {"quantity": Parameter("integer", "Adet", "Emir miktarı", 3, minimum=1)},
                    lambda **_params: None),)
                with patch.dict(sys.modules, {module.__name__: module}):
                    with self.assertRaisesRegex(ValueError, strategy_id):
                        load_registry((module.__name__,))


class StrategyDashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "sample.pcap").write_bytes(capture())
        (root / f"{PLUGIN_MODULE}.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
        self.addCleanup(sys.modules.pop, PLUGIN_MODULE, None)
        with patch.object(sys, "path", [str(root), *sys.path]):
            registry = load_registry((PLUGIN_MODULE,))
            self.plugin = importlib.import_module(PLUGIN_MODULE)
        self.controller = Controller(root, EXECUTABLE, strategy_registry=registry)
        self.addCleanup(self.controller.close)
        server = make_server(self.controller, 0)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def request(self, path, payload=None):
        request = self.base + path
        if payload is not None:
            request = Request(request, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def command(self, action, **fields):
        return self.request("/api/control", {"action": action, **fields})

    def wait_for(self, phase):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.request("/api/state")
            if state["phase"] == phase:
                return state
            if state["phase"] == "error":
                self.fail(state["error"])
            time.sleep(0.01)
        self.fail(f"Oturum aşamasına ulaşılamadı: {phase}")

    def load_capture(self):
        self.command("load", file="sample.pcap", host="239.1.2.3", port=PORT)
        self.wait_for("ready")

    def test_schema_selection_and_fresh_plugin_runs(self):
        catalog = self.request("/api/strategies")
        self.assertEqual(catalog["default_strategy_id"], "two_sided_quote")
        options = {entry["id"]: entry for entry in catalog["strategies"]}
        self.assertTrue({"two_sided_quote", "bid_only", PLUGIN_ID} <= options.keys())
        entry = options[PLUGIN_ID]
        self.assertEqual((entry["version"], entry["api_version"], entry["name"]), ("1.2.3", 1, "Yerel deney"))
        self.assertEqual(entry["parameters"]["quantity"]["help"], "Emir miktarı")
        self.assertIs(entry["parameters"]["fail"]["default"], False)
        initial_state = self.request("/api/state")
        initial = initial_state["strategy_selection"]
        self.assertEqual(initial["id"], "two_sided_quote")
        for strategy_id, params, config in (("unknown", {}, {}),
                                             (PLUGIN_ID, {"quantity": True}, {}),
                                             (PLUGIN_ID, {"quantity": 51}, {}),
                                             (PLUGIN_ID, {"fail": "false"}, {}),
                                             (PLUGIN_ID, {"unexpected": 1}, {}),
                                             (PLUGIN_ID, {"quantity": 42}, {"latency_us": 0}),
                                             (PLUGIN_ID, {"quantity": 3}, {"position_limit": 0})):
            with self.subTest(strategy=strategy_id, parameters=params, config=config):
                with self.assertRaises(HTTPError) as failure:
                    self.command("select_strategy", strategy_id=strategy_id, params=params, config=config)
                self.assertEqual(failure.exception.code, 400)
                state = self.request("/api/state")
                self.assertEqual(state["strategy_selection"], initial)
                self.assertEqual(state["config"], initial_state["config"])

        selected = self.command("select_strategy", strategy_id=PLUGIN_ID, params={}, config={"latency_us": 0})
        self.assertEqual(selected["strategy_selection"], {"id": PLUGIN_ID, "version": "1.2.3",
                                                           "params": {"quantity": 3, "fail": False}})
        self.load_capture()
        cache = self.controller.cache_path
        cache_stamp = cache.stat().st_mtime_ns
        self.command("advance_open")
        opened = self.wait_for("paused")
        self.assertEqual(opened["market_phase"], "P_SUREKLI_ISLEM")
        with self.assertRaises(HTTPError) as failure:
            self.command("select_strategy", strategy_id="bid_only", params={})
        self.assertEqual(failure.exception.code, 409)
        self.assertEqual(self.request("/api/state")["strategy_selection"], selected["strategy_selection"])

        self.command("speed", value="max")
        self.command("start")
        finished = self.wait_for("finished")
        self.assertEqual(finished["strategy"]["inventory"], 3)
        self.assertEqual([(line["type"], line["side"]) for line in finished["journal"]], [("O", "B"), ("A", "B"), ("E", "B")])
        stress = self.command("risk_shock", percent=-1.25)
        mark_price = finished["strategy"]["mark_price"]
        risk = stress["risk"]
        self.assertEqual(risk["base_pnl"], finished["strategy"]["total_pnl"])
        self.assertEqual(risk["rows"][0]["mark_price"], mark_price)
        self.assertEqual(risk["rows"][0]["sensitivity_per_lira"], 3)
        self.assertAlmostEqual(risk["pnl_change"], 3 * mark_price * (-1.25 / 100))
        self.assertEqual(stress["journal"], finished["journal"])
        self.assertEqual(stress["strategy"], finished["strategy"])
        self.assertEqual(stress["progress"]["sequence"], finished["progress"]["sequence"])
        with self.assertRaises(HTTPError) as failure:
            self.command("risk_shock", percent=-101)
        self.assertEqual(failure.exception.code, 400)
        self.assertEqual(self.request("/api/state")["risk"], risk)
        self.command("reset")
        self.command("speed", value=1000)
        self.command("start")
        rerun = self.wait_for("finished")
        self.assertEqual(rerun["journal"], finished["journal"])
        self.assertEqual(rerun["strategy"], finished["strategy"])
        runs = [instance.events for instance in self.plugin.instances if instance.events]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0], runs[1])

        self.command("reset")
        changed = self.command("select_strategy", strategy_id="bid_only", params={})
        self.assertEqual(changed["strategy_selection"]["id"], "bid_only")
        self.assertEqual(self.controller.cache_path, cache)
        self.assertEqual(cache.stat().st_mtime_ns, cache_stamp)

    def test_plugin_exception_preserves_portfolio_and_reset_allows_recovery(self):
        self.load_capture()
        self.command("select_strategy", strategy_id=PLUGIN_ID, params={"quantity": 20, "fail": True},
                     config={"latency_us": 0})
        self.command("speed", value="max")
        self.command("start")
        state = self.wait_for("error")
        self.assertEqual(state["strategy_error"]["id"], PLUGIN_ID)
        self.assertEqual(state["strategy_error"]["sequence"], state["progress"]["sequence"])
        self.assertIn("Yerel strateji deneme hatası", state["error"])
        self.assertEqual((state["strategy"]["inventory"], state["strategy"]["fills"]), (15, 1))
        self.assertIsNone(state["strategy"]["buy_price"])
        self.assertEqual([line["type"] for line in state["journal"]][-2:], ["E", "C"])
        self.assertGreater(state["progress"]["sequence"], 0)
        reset = self.command("reset")
        self.assertEqual(reset["phase"], "ready")
        self.assertEqual(reset["strategy"]["inventory"], 0)
        self.assertEqual(reset["journal"], [])
        self.command("select_strategy", strategy_id=PLUGIN_ID, params={"quantity": 3})
        self.command("start")
        self.assertEqual(self.wait_for("finished")["strategy"]["inventory"], 3)

    def test_learning_strategy_is_optional_and_reports_causal_updates(self):
        catalog = self.request("/api/strategies")
        self.assertEqual(catalog["default_strategy_id"], "two_sided_quote")
        self.assertIn("learning_quote", {entry["id"] for entry in catalog["strategies"]})
        self.assertIsNone(self.request("/api/state")["strategy_diagnostics"])
        (self.controller.root / "learning.pcap").write_bytes(pcap(capture_frames(move_midpoint=True)))
        params = {"minimum_samples": 1, "signal_threshold": 0.51, "quote_interval_ms": 1}
        selected = self.command("select_strategy", strategy_id="learning_quote", params=params)
        self.assertEqual(selected["strategy_diagnostics"]["samples"], 0)
        self.command("load", file="learning.pcap", host="239.1.2.3", port=PORT)
        self.wait_for("ready")
        self.command("speed", value="max")
        self.command("start")
        finished = self.wait_for("finished")
        self.assertEqual(finished["strategy_diagnostics"]["samples"], 1)
        self.assertEqual(finished["strategy_diagnostics"]["last_update"]["label"], 0)
        self.assertEqual(finished["strategy_diagnostics"]["bid_offset_ticks"], 1)
        self.command("reset")
        self.assertEqual(self.request("/api/state")["strategy_diagnostics"]["samples"], 0)
        self.command("speed", value=1000)
        self.command("start")
        replayed = self.wait_for("finished")
        self.assertEqual(replayed["strategy_diagnostics"], finished["strategy_diagnostics"])
        self.assertEqual(replayed["journal"], finished["journal"])


if __name__ == "__main__":
    EXECUTABLE = Path(sys.argv.pop(1)).resolve()
    unittest.main()
