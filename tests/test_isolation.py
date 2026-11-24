import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dashboard.isolation import IsolationUnavailable, _copy_strategy_sources, load_isolated_registry
from dashboard.isolation_protocol import (MAX_FRAME_BYTES, MAX_INTENTS, decode_frame, decode_intents,
                                          encode_frame, encode_intents, event_arguments, event_request)
from dashboard.strategy_api import Cancel, Enter, MarketEvent, OpenOrder, OrderUpdate, Replace, StrategyView


def observation():
    event = MarketEvent("A", 17, 123_456_789, (456, "B", 10200, 15), 21100, 99)
    view = StrategyView(
        symbol="ASELS.E", phase="P_SUREKLI_ISLEM", exchange_ns=event.exchange_ns,
        sequence=event.sequence, best_bid=10200, best_ask=10300,
        best_bid_quantity=15, best_ask_quantity=8, price_decimals=2, round_lot=1,
        position=0, buy_capacity=50, sell_capacity=50,
        orders=(OpenOrder(12, "S", 10400, 4, 0),), pending_sides=("B",), generation=2,
        tick_bands=((0, 0, 100),),
    )
    updates = (OrderUpdate("A", 10, 123_456_000, "S", 12, 9, "ACK", 10400, 4, None),)
    return event, view, updates


class IsolationStagingTests(unittest.TestCase):
    def test_missing_docker_fails_before_import_with_builtins_still_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "imported"
            name = "hft_test_must_not_import"
            (root / f"{name}.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
            )
            with (patch("dashboard.isolation.shutil.which", return_value=None) as find_docker,
                  patch.object(sys, "path", [str(root), *sys.path])):
                self.assertEqual(load_isolated_registry().catalog()["default_strategy_id"], "two_sided_quote")
                find_docker.assert_not_called()
                with self.assertRaisesRegex(IsolationUnavailable, "Docker"):
                    load_isolated_registry((name,), module_root=root)
                find_docker.assert_called_once_with("docker")
                self.assertNotIn(name, sys.modules)
                self.assertFalse(marker.exists())

    def test_staging_only_copies_python_sources_and_has_stable_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            modules = root / "modules"
            package = modules / "quote_plugin"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("# strategy package\n", encoding="utf-8")
            source = package / "signal.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            (package / "helper.py").write_text("OFFSET = 100\n", encoding="utf-8")
            excluded = package / "runtime.txt"
            excluded.write_text("not Python source", encoding="utf-8")
            (package / "recording.pcap").write_bytes(b"fixture")
            cache = package / "__pycache__"
            cache.mkdir()
            (cache / "signal.pyc").write_bytes(b"bytecode fixture")
            (modules / "unselected.py").write_text("# unrelated\n", encoding="utf-8")

            def stage(name, requested):
                destination = root / name
                destination.mkdir()
                digest = _copy_strategy_sources(requested, modules, destination)
                return digest, destination

            first, destination = stage("first", ("quote_plugin.signal", "quote_plugin.helper"))
            plugins = destination / "plugins"
            self.assertEqual({path.relative_to(plugins).as_posix() for path in plugins.rglob("*") if path.is_file()},
                             {"quote_plugin/__init__.py", "quote_plugin/signal.py", "quote_plugin/helper.py"})
            self.assertEqual((plugins / "quote_plugin" / "signal.py").read_bytes(), source.read_bytes())
            self.assertTrue((destination / "dashboard" / "isolation_worker.py").is_file())
            self.assertFalse((destination / "dashboard" / "server.py").exists())
            self.assertRegex(first, r"^[0-9a-f]{64}$")

            excluded.write_text("updated non-source input", encoding="utf-8")
            second, _ = stage("second", ("quote_plugin.helper", "quote_plugin.signal"))
            self.assertEqual(first, second)
            source.write_text("VALUE = 2\n", encoding="utf-8")
            third, _ = stage("third", ("quote_plugin.signal", "quote_plugin.helper"))
            self.assertNotEqual(first, third)

    def test_staging_rejects_source_over_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "large.py").write_bytes(b"VALUE = 1\n")
            destination = root / "staged"
            destination.mkdir()
            with patch("dashboard.isolation.MAX_SOURCE_BYTES", 5), self.assertRaises(ValueError):
                _copy_strategy_sources(("large",), root, destination)


class IsolationProtocolTests(unittest.TestCase):
    def test_exact_frame_bound_and_malformed_frame_rejection(self):
        boundary = "x" * (MAX_FRAME_BYTES - 3)
        frame = encode_frame(boundary)
        self.assertEqual(len(frame), MAX_FRAME_BYTES)
        self.assertEqual(decode_frame(frame), boundary)
        with self.assertRaises(ValueError):
            encode_frame(boundary + "x")
        with self.assertRaises(ValueError):
            encode_frame(float("nan"))
        for malformed in (b"", b'{"ok":null}', b'{"ok":}\n', b"NaN\n", b"Infinity\n",
                          b'"' + b"x" * (MAX_FRAME_BYTES - 2) + b'"\n'):
            with self.subTest(length=len(malformed), prefix=malformed[:8]), self.assertRaises(ValueError):
                decode_frame(malformed)

    def test_intent_types_and_event_round_trip(self):
        intents = (Enter("B", 10200, 2), Replace(12, 10300, 3), Cancel(12))
        self.assertEqual(decode_intents(decode_frame(encode_frame(encode_intents(intents)))), intents)
        valid = encode_intents((intents[0],))[0]
        self.assertEqual(len(decode_intents([valid] * MAX_INTENTS)), MAX_INTENTS)
        for invalid in ((valid,), [valid] * (MAX_INTENTS + 1), [{"kind": "Market", "fields": {}}],
                        [{"kind": "Enter", "fields": []}], [{"kind": "Cancel", "fields": {}, "extra": 1}]):
            with self.subTest(type=type(invalid).__name__, count=len(invalid)), self.assertRaises(ValueError):
                decode_intents(invalid)
        with self.assertRaises(TypeError):
            encode_intents([intents[0]])
        with self.assertRaises(TypeError):
            encode_intents((intents[0],) * (MAX_INTENTS + 1))

        event, view, updates = observation()
        wire = encode_frame(event_request(event, view, updates))
        self.assertEqual(event_arguments(decode_frame(wire)), (event, view, updates))


@unittest.skipUnless(os.environ.get("HFT_TEST_DOCKER") == "1", "requires an opted-in local Linux Docker engine")
class DockerIsolationLifecycleTests(unittest.TestCase):
    def test_real_catalog_worker_and_cleanup(self):
        module_root = Path(__file__).resolve().parents[1]
        registry = load_isolated_registry(("examples.asels_alis",), module_root=module_root)
        self.addCleanup(registry.close)
        self.assertRegex(registry.fingerprints["bid_imbalance"], r"^[0-9a-f]{64}$")
        definition = registry.get("bid_imbalance")
        strategy = definition.create(definition.resolve({"quantity": 3}))
        self.addCleanup(strategy.close)
        self.assertIsNone(strategy.worker)
        event, view, updates = observation()
        view = replace(view, orders=(), pending_sides=())
        self.assertEqual(strategy.on_event(event, view, updates), (Enter("B", 10200, 3),))
        staging_root = registry._sandbox.root
        self.assertTrue(staging_root.is_dir())
        strategy.close()
        self.assertIsNone(strategy.worker)
        with self.assertRaises(RuntimeError):
            strategy.on_event(event, view, ())
        registry.close()
        self.assertFalse(staging_root.exists())


if __name__ == "__main__":
    unittest.main()
