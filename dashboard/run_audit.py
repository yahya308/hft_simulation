"""Persist replay provenance, a sequenced ledger, and result fingerprints."""

from datetime import datetime, timezone
from contextlib import contextmanager
from errno import EACCES, EAGAIN, EDEADLK
from hashlib import sha256
import json
import os
from pathlib import Path
from re import fullmatch
from time import monotonic
from uuid import uuid4


_CHECKPOINT_RECORDS = 4_096
_CHECKPOINT_SECONDS = 1.0
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_OWNER_LOCK_OFFSET = 63


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def file_sha256(path: Path, *, check=None) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            if check is not None:
                check()
            digest.update(block)
    return digest.hexdigest()


def source_sha256(root: Path) -> str:
    files = {path.relative_to(root).as_posix(): file_sha256(path)
             for path in sorted(root.rglob("*.py"))}
    return sha256(canonical_json(files)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_directory(path: Path) -> None:
    # POSIX can flush directory entry changes after an atomic replacement.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_json(path: Path, value: dict) -> None:
    draft = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with draft.open("xb") as stream:
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        draft.replace(path)
        _sync_directory(path.parent)
    finally:
        draft.unlink(missing_ok=True)


def _try_owner_lock(stream) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(_OWNER_LOCK_OFFSET)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (EACCES, EAGAIN, EDEADLK):
            return False
        raise
    return True


@contextmanager
def _observe_owner(directory: Path, run_id: str):
    """Keep the run lock held across recovery operations when the writer is gone."""
    stream = None
    state = "unknown"
    try:
        stream = (directory / "owner.lock").open("r+b", buffering=0)
        marker = (run_id + "\n").encode("utf-8")
        if len(marker) < _OWNER_LOCK_OFFSET and stream.read(len(marker)) == marker:
            state = "released" if _try_owner_lock(stream) else "active"
    except OSError:
        pass
    try:
        yield state
    finally:
        if stream is not None:
            stream.close()


def _read_manifest(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or not isinstance(manifest.get("id"), str)):
        raise ValueError("Koşu kaydının sürümü veya kimliği desteklenmiyor.")
    if sha256(canonical_json(manifest["scenario"])).hexdigest() != manifest.get("scenario_sha256"):
        raise ValueError("Koşu girdilerinin SHA-256 özeti uyuşmuyor.")
    return manifest


class RunAudit:
    def __init__(self, root: Path, scenario: dict, *, checkpoint_records: int = _CHECKPOINT_RECORDS,
                 checkpoint_seconds: float = _CHECKPOINT_SECONDS):
        if checkpoint_records < 1 or checkpoint_seconds <= 0:
            raise ValueError("Geçersiz koşu kontrol noktası aralığı.")
        self.id = uuid4().hex
        self.directory = root / self.id
        self.directory.mkdir(parents=True)
        _sync_directory(self.directory.parent)
        scenario_bytes = canonical_json(scenario)
        self.manifest = {"schema": 1, "id": self.id, "created_utc": _utc_now(),
                         "status": "running", "scenario": json.loads(scenario_bytes),
                         "scenario_sha256": sha256(scenario_bytes).hexdigest(), "checkpoint_schema": 1}
        self.digest = sha256()
        self.count = 0
        self.byte_length = 0
        self.checkpoint_records = checkpoint_records
        self.checkpoint_seconds = checkpoint_seconds
        self.checkpoint_count = 0
        self.checkpoint_state = None
        self.checkpoint_time = monotonic()
        self.ledger_write_failed = False
        self.stream = self.owner = None
        try:
            self.owner = (self.directory / "owner.lock").open("x+b", buffering=0)
            self.owner.write((self.id + "\n").encode("ascii").ljust(_OWNER_LOCK_OFFSET + 1, b"\0"))
            os.fsync(self.owner.fileno())
            if not _try_owner_lock(self.owner):
                raise OSError("Koşu sahiplik kilidi alınamadı.")
            self.stream = (self.directory / "events.ndjson").open("xb", buffering=128 * 1024)
            self.checkpoint()
            _write_json(self.directory / "manifest.json", self.manifest)
        except BaseException:
            self._close_streams()
            raise

    def record(self, kind: str, fields: dict) -> None:
        if self.stream is None:
            return
        if self.ledger_write_failed:
            raise OSError("Koşu günlüğünde tamamlanmamış bir yazma işlemi var.")
        if not isinstance(kind, str) or not isinstance(fields, dict):
            raise ValueError("Koşu günlüğü kayıt türü veya verisi geçersiz.")
        line = canonical_json({"index": self.count + 1, "kind": kind, "data": fields}) + b"\n"
        if len(line) > _MAX_RECORD_BYTES:
            raise ValueError("Koşu günlüğü satırı boyut sınırını aşıyor.")
        try:
            if self.stream.write(line) != len(line):
                raise OSError("Koşu günlüğü satırı tamamlanamadı.")
        except OSError:
            self.ledger_write_failed = True
            raise
        self.digest.update(line)
        self.count += 1
        self.byte_length += len(line)
        if (self.count - self.checkpoint_count >= self.checkpoint_records
                or monotonic() - self.checkpoint_time >= self.checkpoint_seconds):
            self.checkpoint()

    def checkpoint(self) -> dict:
        if self.stream is None:
            raise ValueError("Koşu günlüğü açık değil.")
        if self.ledger_write_failed:
            raise OSError("Koşu günlüğünde tamamlanmamış bir yazma işlemi var.")
        if self.checkpoint_state is not None and self.count == self.checkpoint_count:
            return self.checkpoint_state.copy()
        self.stream.flush()
        os.fsync(self.stream.fileno())
        if self.stream.tell() != self.byte_length:
            raise OSError("Koşu günlüğünün yazılan uzunluğu uyuşmuyor.")
        current = {"schema": 1, "run_id": self.id, "records": self.count, "last_index": self.count,
                   "bytes": self.byte_length, "events_sha256": self.digest.hexdigest()}
        _write_json(self.directory / "checkpoint.json", current)
        self.checkpoint_state = current
        self.checkpoint_count, self.checkpoint_time = self.count, monotonic()
        return current.copy()

    def _close_streams(self) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.stream = None
            if self.owner is not None:
                self.owner.close()
                self.owner = None

    def finish(self, status: str, results: dict, *, error: str | None = None) -> None:
        if self.stream is None:
            return
        if status in ("running", "interrupted"):
            raise ValueError("Kapanış için geçersiz koşu durumu.")
        try:
            checkpoint = self.checkpoint()
            self.stream.close()
            manifest = self.manifest | {"status": status, "results": results, "error": error,
                                       "records": self.count, "events_sha256": checkpoint["events_sha256"],
                                       "results_sha256": sha256(canonical_json(results)).hexdigest()}
            _write_json(self.directory / "manifest.json", manifest)
            self.manifest = manifest
        finally:
            self._close_streams()

    def public(self) -> dict:
        return {"id": self.id, "status": self.manifest["status"], "records": self.count,
                "events_sha256": self.manifest.get("events_sha256"), "checkpoint_records": self.checkpoint_count}


def _read_checkpoint(directory: Path, manifest: dict) -> dict:
    if manifest.get("checkpoint_schema") != 1:
        raise ValueError("Bu koşu sürümünde kontrol noktası bulunmuyor.")
    checkpoint = json.loads((directory / "checkpoint.json").read_bytes())
    if (not isinstance(checkpoint, dict) or checkpoint.get("schema") != 1
            or checkpoint.get("run_id") != manifest["id"]
            or type(checkpoint.get("records")) is not int
            or type(checkpoint.get("last_index")) is not int
            or not 0 <= checkpoint["records"] == checkpoint["last_index"]
            or type(checkpoint.get("bytes")) is not int or checkpoint["bytes"] < 0
            or not isinstance(checkpoint.get("events_sha256"), str)
            or not fullmatch(r"[0-9a-f]{64}", checkpoint["events_sha256"])):
        raise ValueError("Koşu kontrol noktası geçersiz.")
    if manifest.get("status") == "interrupted" and manifest.get("checkpoint") != checkpoint:
        raise ValueError("Kesintili koşunun manifesti kontrol noktasıyla uyuşmuyor.")
    return checkpoint


def _verify_prefix(ledger: Path, checkpoint: dict, output=None) -> int:
    remaining = checkpoint["bytes"]
    digest = sha256()
    records = 0
    with ledger.open("rb") as stream:
        while remaining:
            line = stream.readline(min(remaining, _MAX_RECORD_BYTES + 1))
            if len(line) > _MAX_RECORD_BYTES or not line.endswith(b"\n"):
                raise ValueError("Kontrol noktası tam günlük satırlarına karşılık gelmiyor.")
            remaining -= len(line)
            records += 1
            entry = json.loads(line)
            if (not isinstance(entry, dict) or type(entry.get("index")) is not int or entry["index"] != records
                    or not isinstance(entry.get("kind"), str) or not isinstance(entry.get("data"), dict)):
                raise ValueError("Kontrol noktasına kadar günlük sıra numaraları geçersiz.")
            digest.update(line)
            if output is not None and output.write(line) != len(line):
                raise OSError("Kurtarılan günlük satırı yazılamadı.")
        total_bytes = os.fstat(stream.fileno()).st_size
    if (records != checkpoint["records"] or total_bytes < checkpoint["bytes"]
            or digest.hexdigest() != checkpoint["events_sha256"]):
        raise ValueError("Emir günlüğünün boyutu, kayıt sayısı veya SHA-256 özeti kontrol noktasıyla uyuşmuyor.")
    return total_bytes


def _verify_closed(directory: Path, manifest: dict) -> dict:
    ledger = directory / "events.ndjson"
    if "checkpoint_schema" in manifest:
        checkpoint = _read_checkpoint(directory, manifest)
        if (checkpoint["events_sha256"] != manifest.get("events_sha256")
                or checkpoint["records"] != manifest.get("records")
                or _verify_prefix(ledger, checkpoint) != checkpoint["bytes"]):
            raise ValueError("Emir günlüğünün boyutu, kayıt sayısı veya SHA-256 özeti kapanış manifestiyle uyuşmuyor.")
    elif file_sha256(ledger) != manifest.get("events_sha256"):
        raise ValueError("Emir günlüğü SHA-256 özeti uyuşmuyor.")
    if sha256(canonical_json(manifest["results"])).hexdigest() != manifest.get("results_sha256"):
        raise ValueError("Koşu sonucu SHA-256 özeti uyuşmuyor.")
    return manifest


def verify_run(directory: Path) -> dict:
    manifest = _read_manifest(directory)
    if manifest.get("status") in ("running", "interrupted", None):
        raise ValueError("Koşu kaydı tamamlanmış bir kapanış içermiyor. Durumu --inspect ile incele.")
    return _verify_closed(directory, manifest)


def inspect_run(directory: Path) -> dict:
    """Verify sealed results or only a durable prefix, without rewriting the run."""
    manifest = _read_manifest(directory)
    if manifest["status"] in ("running", "interrupted"):
        with _observe_owner(directory, manifest["id"]) as owner:
            # A finish may have committed its manifest just before the owner released the lock.
            if owner == "released":
                manifest = _read_manifest(directory)
            if manifest["status"] in ("running", "interrupted"):
                status = "interrupted" if owner == "released" else manifest["status"]
                checkpoint = None
                tail_bytes = None
                if "checkpoint_schema" in manifest:
                    checkpoint = _read_checkpoint(directory, manifest)
                    tail_bytes = _verify_prefix(directory / "events.ndjson", checkpoint) - checkpoint["bytes"]
                return {"id": manifest["id"], "status": status, "manifest_status": manifest["status"],
                        "owner": owner, "checkpoint": checkpoint, "uncheckpointed_bytes": tail_bytes,
                        "recoverable": status == "interrupted" and checkpoint is not None}
    _verify_closed(directory, manifest)
    return {"id": manifest["id"], "status": manifest["status"], "manifest_status": manifest["status"],
            "records": manifest["records"], "events_sha256": manifest["events_sha256"],
            "results_sha256": manifest["results_sha256"], "closed": True}


def _seal_released_run(directory: Path, manifest: dict) -> dict:
    checkpoint = _read_checkpoint(directory, manifest)
    tail_bytes = _verify_prefix(directory / "events.ndjson", checkpoint) - checkpoint["bytes"]
    interrupted = manifest | {"status": "interrupted", "interrupted_utc": _utc_now(),
                             "checkpoint": checkpoint, "uncheckpointed_bytes": tail_bytes}
    _write_json(directory / "manifest.json", interrupted)
    return interrupted


def seal_interrupted(directory: Path) -> dict:
    """Mark an abandoned run as interrupted while retaining all source ledger bytes."""
    manifest = _read_manifest(directory)
    with _observe_owner(directory, manifest["id"]) as owner:
        if owner == "released":
            manifest = _read_manifest(directory)
        if manifest["status"] == "interrupted":
            _verify_prefix(directory / "events.ndjson", _read_checkpoint(directory, manifest))
            return manifest
        if manifest["status"] != "running" or owner != "released":
            raise ValueError("Yalnız yazarı kalmamış açık bir koşu kesintili olarak işaretlenebilir.")
        return _seal_released_run(directory, manifest)


def seal_interrupted_runs(root: Path) -> list[dict]:
    """Reconcile abandoned runs on startup. Active, legacy, and corrupt files stay intact."""
    if not root.is_dir():
        return []
    changes = []
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not (directory / "owner.lock").is_file():
            continue
        try:
            manifest = _read_manifest(directory)
            if manifest["status"] != "running":
                continue
            with _observe_owner(directory, manifest["id"]) as owner:
                if owner == "released":
                    manifest = _read_manifest(directory)
                    if manifest["status"] == "running":
                        changes.append(_seal_released_run(directory, manifest))
        except (OSError, ValueError, KeyError) as error:
            changes.append({"id": directory.name, "status": "unverified", "error": str(error)})
    return changes


def recover_run(directory: Path, destination: Path) -> dict:
    """Export the durable prefix to a new run folder, with interrupted status."""
    directory, destination = directory.resolve(), destination.resolve()
    if destination.is_relative_to(directory):
        raise ValueError("Kurtarma çıktısı kaynak koşu dizininin dışında olmalı.")
    manifest = _read_manifest(directory)
    ledger = directory / "events.ndjson"
    with _observe_owner(directory, manifest["id"]) as owner:
        if owner == "released":
            manifest = _read_manifest(directory)
        if manifest["status"] != "interrupted" and not (manifest["status"] == "running" and owner == "released"):
            raise ValueError("Yalnız kesintiye uğramış bir koşu kurtarılabilir.")
        checkpoint = _read_checkpoint(directory, manifest)
        source_bytes = _verify_prefix(ledger, checkpoint)
        destination.mkdir(parents=True)
        with (destination / "events.ndjson").open("xb") as output:
            _verify_prefix(ledger, checkpoint, output)
            output.flush()
            os.fsync(output.fileno())
        recovered = manifest | {"status": "interrupted", "checkpoint": checkpoint, "uncheckpointed_bytes": 0,
                                "recovery": {"source_id": manifest["id"], "source_bytes": source_bytes,
                                             "recovered_utc": _utc_now()}}
        _write_json(destination / "checkpoint.json", checkpoint)
        _write_json(destination / "manifest.json", recovered)
        _sync_directory(destination.parent)
    return recovered
