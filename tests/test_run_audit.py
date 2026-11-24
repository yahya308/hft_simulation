import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dashboard.run_audit import (RunAudit, canonical_json, inspect_run, recover_run, seal_interrupted,
                                 seal_interrupted_runs, verify_run)


class RunAuditTest(unittest.TestCase):
    def test_checkpoint_and_sealed_audit_can_be_verified(self):
        with TemporaryDirectory() as folder:
            scenario = {"symbols": ["ASELS.E"]}
            audit = RunAudit(Path(folder), scenario, checkpoint_records=2, checkpoint_seconds=600)
            try:
                initial = inspect_run(audit.directory)
                self.assertEqual((initial["status"], initial["owner"], initial["checkpoint"]["records"]),
                                 ("running", "active", 0))
                scenario["symbols"].append("THYAO.E")
                audit.record("itch", {"source_sequence": 10})
                self.assertEqual(audit.public()["checkpoint_records"], 0)
                audit.record("itch", {"source_sequence": 11})
                checkpoint = inspect_run(audit.directory)["checkpoint"]
                self.assertEqual((checkpoint["records"], checkpoint["last_index"]), (2, 2))
                ledger = audit.directory / "events.ndjson"
                self.assertEqual(ledger.stat().st_size, checkpoint["bytes"])
                self.assertEqual(sha256(ledger.read_bytes()).hexdigest(), checkpoint["events_sha256"])
                self.assertEqual(seal_interrupted_runs(Path(folder)), [])
                with self.assertRaisesRegex(ValueError, "yazarı kalmamış"):
                    seal_interrupted(audit.directory)
            finally:
                audit.finish("finished", {"ASELS.E": {"position": 0}})
            manifest = verify_run(audit.directory)
            self.assertEqual((manifest["status"], manifest["scenario"]["symbols"]), ("finished", ["ASELS.E"]))
            self.assertTrue(inspect_run(audit.directory)["closed"])

    def test_crash_seal_and_prefix_export_retain_original_tail(self):
        code = """
import os
from pathlib import Path
from dashboard.run_audit import RunAudit
import sys

audit = RunAudit(Path(sys.argv[1]), {"day": "2026-05-22"}, checkpoint_records=2, checkpoint_seconds=600)
for index in range(3):
    audit.record("itch", {"sequence": index})
audit.stream.write(b'{"index":4')
audit.stream.flush()
print(audit.id, flush=True)
os._exit(17)
"""
        with TemporaryDirectory() as folder:
            root = Path(folder)
            child = subprocess.run([sys.executable, "-B", "-c", code, str(root)], capture_output=True, text=True,
                                   timeout=15)
            self.assertEqual(child.returncode, 17, child.stderr)
            source = root / child.stdout.strip()
            manifest_file = source / "manifest.json"
            ledger = source / "events.ndjson"
            original = ledger.read_bytes()
            before = manifest_file.read_bytes()
            inspection = inspect_run(source)
            checkpoint = inspection["checkpoint"]
            self.assertEqual((inspection["status"], inspection["manifest_status"], inspection["owner"]),
                             ("interrupted", "running", "released"))
            self.assertEqual(checkpoint["records"], 2)
            self.assertGreater(inspection["uncheckpointed_bytes"], 0)
            self.assertEqual(manifest_file.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "tamamlanmış"):
                verify_run(source)

            changes = seal_interrupted_runs(root)
            self.assertEqual([item["id"] for item in changes], [source.name])
            self.assertEqual(changes[0]["status"], "interrupted")
            self.assertNotIn("results", changes[0])
            self.assertEqual(seal_interrupted_runs(root), [])

            destination = root / "export"
            recovered = recover_run(source, destination)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual((destination / "events.ndjson").read_bytes(), original[:checkpoint["bytes"]])
            self.assertEqual(ledger.read_bytes(), original)
            self.assertEqual(inspect_run(destination)["uncheckpointed_bytes"], 0)
            with self.assertRaises(FileExistsError):
                recover_run(source, destination)
            with self.assertRaisesRegex(ValueError, "dışında"):
                recover_run(source, source / "export")
            with self.assertRaisesRegex(ValueError, "tamamlanmış"):
                verify_run(destination)

    def test_corrupt_prefix_is_neither_sealed_nor_exported(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            audit = RunAudit(root, {})
            try:
                audit.record("itch", {"sequence": 1})
                audit.checkpoint()
            finally:
                audit._close_streams()
            ledger = audit.directory / "events.ndjson"
            ledger.write_bytes(ledger.read_bytes().replace(b'"index":1', b'"index":3'))
            manifest = audit.directory / "manifest.json"
            old = manifest.read_bytes()
            self.assertEqual(seal_interrupted_runs(root)[0]["status"], "unverified")
            with self.assertRaisesRegex(ValueError, "sıra numaraları"):
                recover_run(audit.directory, root / "export")
            self.assertEqual(manifest.read_bytes(), old)
            self.assertFalse((root / "export").exists())

    def test_legacy_closed_stays_readable_and_open_has_unknown_owner(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ledger = b'{"data":{},"index":1,"kind":"itch"}\n'
            (root / "events.ndjson").write_bytes(ledger)
            scenario, results = {"quotes": 2}, {"position": 0}
            manifest = {"id": "old-run", "schema": 1, "status": "finished", "records": 1,
                        "scenario": scenario, "results": results, "events_sha256": sha256(ledger).hexdigest(),
                        "scenario_sha256": sha256(canonical_json(scenario)).hexdigest(),
                        "results_sha256": sha256(canonical_json(results)).hexdigest()}
            path = root / "manifest.json"
            path.write_bytes(canonical_json(manifest))
            self.assertEqual(verify_run(root), manifest)
            path.write_bytes(canonical_json(manifest | {"status": "running"}))
            inspection = inspect_run(root)
            self.assertEqual((inspection["status"], inspection["owner"], inspection["recoverable"]),
                             ("running", "unknown", False))
            with self.assertRaises(ValueError):
                seal_interrupted(root)

    def test_failed_checkpoint_write_preserves_last_claimed_prefix(self):
        with TemporaryDirectory() as folder:
            audit = RunAudit(Path(folder), {}, checkpoint_records=2, checkpoint_seconds=600)
            stream = audit.directory / "checkpoint.json"
            initial = stream.read_bytes()
            try:
                audit.record("itch", {"sequence": 1})
                with patch("dashboard.run_audit._write_json", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        audit.record("itch", {"sequence": 2})
                self.assertEqual(stream.read_bytes(), initial)
            finally:
                audit._close_streams()
            report = inspect_run(audit.directory)
            self.assertEqual((report["status"], report["checkpoint"]["records"]), ("interrupted", 0))

    def test_partial_record_write_prevents_closing_manifest(self):
        class PartialWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def write(self, line):
                return self.wrapped.write(line[:5])

            def close(self):
                self.wrapped.close()

        with TemporaryDirectory() as folder:
            audit = RunAudit(Path(folder), {}, checkpoint_records=1)
            audit.record("itch", {"sequence": 1})
            audit.stream = PartialWriter(audit.stream)
            with self.assertRaises(OSError):
                audit.record("itch", {"sequence": 2})
            with self.assertRaises(OSError):
                audit.finish("error", {})
            report = inspect_run(audit.directory)
            self.assertEqual((report["status"], report["checkpoint"]["records"],
                              report["uncheckpointed_bytes"]), ("interrupted", 1, 5))

    def test_elapsed_interval_publishes_even_with_few_records(self):
        with TemporaryDirectory() as folder, patch("dashboard.run_audit.monotonic") as clock:
            clock.return_value = 100.0
            audit = RunAudit(Path(folder), {}, checkpoint_records=100, checkpoint_seconds=1)
            try:
                audit.record("itch", {"sequence": 1})
                self.assertEqual(audit.public()["checkpoint_records"], 0)
                clock.return_value = 101.0
                audit.record("itch", {"sequence": 2})
                self.assertEqual(audit.public()["checkpoint_records"], 2)
            finally:
                audit.finish("finished", {})


if __name__ == "__main__":
    unittest.main()
