import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .external_replay import run_results, submit_command
from .market_feed import MarketFeed
from .multi_simulation import MultiSimulation
from .ouch import Reply
from .portfolio_risk import parse_shock_percent
from .replay_stream import ReplayStream
from .run_audit import RunAudit, file_sha256, seal_interrupted_runs, source_sha256
from .scenario import EXTRA_DEFAULTS, EXTRA_LIMITS
from .simulation import canonical_symbol
from .strategy_api import API_VERSION, StrategyError
from .strategy_registry import DEFAULT_STRATEGY, LEARNING_QUOTE_STRATEGY, StrategyDefinition, load_registry


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
ISTANBUL = timezone(timedelta(hours=3))
NS = 1_000_000_000
DEFAULT_HOST, DEFAULT_PORT = "233.113.216.71", 21001
EQUITY_PORTS = (21001, 21002, 21003, 21004)
DEFAULT_SYMBOL = "ASELS.E"
DEFAULTS = {"position_limit": 100, "latency_us": 100, "fee_bps": 0, **EXTRA_DEFAULTS}
LIMITS = {"position_limit": (1, 1_000_000), "latency_us": (0, 10_000_000), "fee_bps": (0, 1_000), **EXTRA_LIMITS}
EXTERNAL_STRATEGY = "external_ouch"


class ExternalStrategy:
    def on_event(self, event, view, updates):
        return ()


EXTERNAL_DEFINITION = StrategyDefinition(EXTERNAL_STRATEGY, "1.0.0", API_VERSION, "Harici OUCH istemcisi",
    "Yerel SoupBinTCP oturumundan gelen pasif Pay Piyasası emirlerini kullanır.", {}, ExternalStrategy)


def clock_text(value):
    if not value:
        return None
    seconds, fraction = divmod(int(value), NS)
    wall = datetime.fromtimestamp(seconds, ISTANBUL)
    return f"{wall:%H:%M:%S}.{fraction // 1_000_000:03d}"


class StrategySelectionConflict(ValueError):
    pass


@dataclass(slots=True)
class EmergencyRequest:
    run_id: str
    event: threading.Event = field(default_factory=threading.Event)
    active: bool = True
    requested_ns: int | None = None
    applied_ns: int | None = None


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} {minimum} ile {maximum} arasında bir tam sayı olmalı.")
    return value


class PlaybackClock:
    def __init__(self, speed=1):
        self.speed = speed
        self.market_ns = 0
        self.wall_ns = 0

    def restart(self, market_ns, wall_ns, speed=None):
        self.market_ns = market_ns
        self.wall_ns = wall_ns
        if speed is not None:
            self.speed = speed

    def market_at(self, wall_ns):
        if self.speed == "max":
            return self.market_ns
        return self.market_ns + max(0, wall_ns - self.wall_ns) * self.speed

    def seconds_until(self, market_ns, wall_ns):
        if self.speed == "max":
            return 0
        return max(0, market_ns - self.market_at(wall_ns)) / (self.speed * NS)


class Controller:
    def __init__(self, root=ROOT, extractor=None, strategy_registry=None):
        self.root = Path(root).resolve()
        name = "hft_asels_feed.exe" if os.name == "nt" else "hft_asels_feed"
        self.extractor = Path(extractor or self.root / "build" / name).resolve()
        self.cache_dir = self.root / "build" / "dashboard-cache"
        self.run_dir = self.root / "build" / "runs"
        self.interrupted_runs = seal_interrupted_runs(self.run_dir)
        self.run_audit = None
        self.capture_identity = None
        self.extractor_sha256 = None
        self.engine_sha256 = source_sha256(Path(__file__).parent)
        self.coverage = {}
        self.recoveries = {}
        self.gateway = None
        self.market_feed = None
        self.condition = threading.Condition()
        self._emergency_lock = threading.Lock()
        self._emergency_wakeup = threading.Event()
        self._emergency = None
        self.closed = False
        self.phase, self.error = "idle", None
        self.file, self.host, self.port = "", DEFAULT_HOST, DEFAULT_PORT
        self.config, self.enabled = DEFAULTS.copy(), True
        self.shock_percent = parse_shock_percent(-1)
        self.strategy_registry = load_registry() if strategy_registry is None else strategy_registry
        self.strategy_definition = self.strategy_registry.get(DEFAULT_STRATEGY)
        self.strategy_params = self.strategy_definition.resolve({})
        self.strategy_error = None
        self.strategy_controls = deque(maxlen=80)
        self.clock = PlaybackClock(1)
        self.ticket, self.preparations = 0, set()
        self.catalog_ticket, self.catalog_process = 0, None
        self.catalog = {"phase": "idle", "instruments": []}
        self.symbols = (DEFAULT_SYMBOL,)
        self.routes = {DEFAULT_SYMBOL: DEFAULT_PORT}
        self.focus_symbol = DEFAULT_SYMBOL
        self.bytes_read, self.total_bytes = 0, 0
        self.prepared_bytes = {}
        self.cache_path, self.stream, self.pending = None, None, None
        self.cache_paths = {}
        self.sim = self._simulation()
        self.sequence, self.market_ns, self.events = 0, 0, 0
        self.source_port = self.source_sequence = None
        self.started = None
        self.paused_market_ns = 0
        self.seeking_open = False
        self.open_target = None
        self.speed_sample, self.actual_speed = None, 0
        self.history, self.last_history = {symbol: deque(maxlen=600) for symbol in self.symbols}, 0
        self.worker = threading.Thread(target=self._replay, daemon=True)
        self.worker.start()
        self._emergency_worker = threading.Thread(target=self._drain_emergency, daemon=True)
        self._emergency_worker.start()

    def _simulation(self):
        return self._create_simulation(self.strategy_definition, self.strategy_params, self.config)

    def _create_simulation(self, definition, params, config, routes=None):
        if (definition.id in (DEFAULT_STRATEGY, "bid_only", LEARNING_QUOTE_STRATEGY)
                and params["quantity"] > config["position_limit"]):
            raise ValueError("Emir miktarı pozisyon limitini aşamaz.")
        routes = self.routes if routes is None else routes
        settings = {**config, "enabled": self.enabled}
        settings["max_position"] = settings.pop("position_limit")
        return MultiSimulation({symbol: settings for symbol in routes}, sources=routes,
                               strategy_factory=lambda _symbol: definition.create(params))

    def _select_strategy(self, strategy_id, params, updates):
        if self.phase not in ("idle", "ready"):
            raise StrategySelectionConflict("Strateji veya ayarları değiştirmek için önce Başa al düğmesini kullan.")
        if not isinstance(updates, dict) or updates.keys() - LIMITS.keys():
            raise ValueError("Bilinmeyen ortak simülasyon ayarı.")
        if self.sim.stop_reason is not None:
            raise StrategySelectionConflict("Risk durdurması kilitli. Önce replay’i başa al.")
        definition = (EXTERNAL_DEFINITION if self.gateway is not None and strategy_id == EXTERNAL_STRATEGY
                      else self.strategy_registry.get(strategy_id))
        params = definition.resolve(params)
        config = self.config | {key: integer(value, *LIMITS[key], key) for key, value in updates.items()}
        if (definition.id, params, config) == (self.strategy_definition.id, self.strategy_params, self.config):
            return
        sim = self._create_simulation(definition, params, config)
        self.sim.close_strategies()
        self.strategy_definition, self.strategy_params, self.config, self.sim = definition, params, config, sim

    def strategy_catalog(self):
        catalog = self.strategy_registry.catalog()
        if self.gateway is not None:
            catalog["strategies"].append(EXTERNAL_DEFINITION.metadata())
        return catalog

    def capture_files(self):
        return sorted(item.name for item in self.root.glob("*.pcap")
                      if item.is_file() and item.resolve().parent == self.root)

    def cache_file(self, capture, host, port, symbols=(DEFAULT_SYMBOL,), recovery=None):
        stat = capture.stat()
        archive = None
        if recovery:
            path, address, udp_port = recovery
            archived = path.stat()
            archive = (path.name, archived.st_size, archived.st_mtime_ns, address, udp_port)
        identity = (capture.name, stat.st_size, stat.st_mtime_ns, host, port,
                    tuple(sorted(symbols)), self.extractor.stat().st_mtime_ns, archive, "equity-itch-v2112-v2")
        return self.cache_dir / (hashlib.sha256(repr(identity).encode()).hexdigest() + ".ndjson")

    def _cancel_preparation(self):
        self.ticket += 1
        for process in self.preparations:
            if process.poll() is None:
                process.terminate()

    def _reset(self, sim=None):
        sim = self._simulation() if sim is None else sim
        stream = ReplayStream(self.cache_paths) if self.cache_paths else None
        try:
            pending = stream.read() if stream else None
            if stream and pending is None:
                raise ValueError("Seçilen payların olay dosyası boş.")
        except Exception:
            if stream:
                stream.close()
            raise
        if self.stream:
            self.stream.close()
        if self.run_audit is not None:
            self._seal_run("aborted")
        self.sim.close_strategies()
        if self.gateway is not None and self.started is not None:
            self.gateway.reset_session()
        self.run_audit = None
        self.market_feed = None
        with self._emergency_lock:
            self._emergency = None
        self.stream, self.pending = stream, pending
        self.sim = sim
        self.sequence, self.market_ns, self.events = 0, 0, 0
        self.source_port = self.source_sequence = None
        self.started = None
        self.paused_market_ns = 0
        self.seeking_open = False
        self.open_target = None
        self.speed_sample, self.actual_speed = None, 0
        self.history = {symbol: deque(maxlen=600) for symbol in self.symbols}
        self.last_history = 0
        self.error = None
        self.strategy_error = None
        self.strategy_controls.clear()
        self.phase = "ready" if pending else "idle"

    def _begin_run(self):
        self.external_count = 0
        self.external_reply_count = 0
        strategy = {"id": self.strategy_definition.id, "version": self.strategy_definition.version,
                    "params": self.strategy_params}
        fingerprints = getattr(self.strategy_registry, "fingerprints", {})
        if strategy["id"] in fingerprints:
            strategy["sha256"] = fingerprints[strategy["id"]]
            strategy["runtime_image"] = getattr(self.strategy_registry, "runtime_image", None)
        self.run_audit = RunAudit(self.run_dir, {"capture": self.capture_identity, "coverage": self.coverage,
            "sources": {"host": self.host, "routes": self.routes}, "strategy": strategy,
            "settings": self.config, "enabled": self.enabled, "engine_sha256": self.engine_sha256,
            "extractor_sha256": self.extractor_sha256, "python": platform.python_version(), "external_ledger": 2})
        self.sim.set_audit_sink(self.run_audit.record)
        emergency = EmergencyRequest(self.run_audit.id)
        # External sessions have no in-engine strategy callback. Complete the current
        # dispatch before canceling so the boundary can be replayed exactly.
        self.sim.set_emergency_event(emergency.event, atomic_dispatch=strategy["id"] == EXTERNAL_STRATEGY)
        with self._emergency_lock:
            self._emergency = emergency
        if strategy["id"] == EXTERNAL_STRATEGY:
            generation = self.gateway.generation
            self.market_feed = MarketFeed(self.config)
            self.sim.set_reply_sink(lambda frame: self._publish_external(frame, generation),
                context_sink=lambda frame, symbol, sequence, delivery: self._publish_external(
                    frame, generation, symbol=symbol, local_sequence=sequence, delivery=delivery))

    def _publish_external(self, frame, generation, *, symbol=None, local_sequence=None, delivery=None):
        transport_sequence = self.gateway.publish(frame, generation=generation)
        if transport_sequence is not None:
            self.external_reply_count += 1
            self.run_audit.record("external_reply", {"sequence": self.external_reply_count, "frame_hex": frame.hex(),
                "symbol": symbol, "local_ouch_sequence": local_sequence, "transport_sequence": transport_sequence,
                "delivery_time_ns": str(delivery) if delivery is not None else None})

    def _seal_run(self, status, error=None):
        if self.run_audit is None or self.run_audit.stream is None:
            return
        with self._emergency_lock:
            if self._emergency is not None:
                self._emergency.active = False
        self._settle_emergency()
        self.sim.finish()
        self.run_audit.finish(status, run_results(self.sim), error=error)
        self.sim.close_strategies()
        if self.gateway is not None and self.strategy_definition.id == EXTERNAL_STRATEGY:
            self.gateway.end_session()

    def request_emergency(self, run_id=None):
        """Latch immediately without waiting for the replay/strategy lock."""
        with self._emergency_lock:
            request = self._emergency
            if request is None or not request.active or self.closed:
                raise StrategySelectionConflict("Acil durdurma aktif replay sırasında kullanılabilir.")
            if run_id is not None and run_id != request.run_id:
                raise StrategySelectionConflict("Acil durdurma eski bir koşuya ait.")
            if request.requested_ns is None:
                request.requested_ns = time.monotonic_ns()
                request.event.set()
                self._emergency_wakeup.set()
            return {"run_id": request.run_id, "accepted": True, "applied": request.applied_ns is not None}

    def _settle_emergency(self):
        with self._emergency_lock:
            request = self._emergency
            pending = request is not None and request.event.is_set() and request.applied_ns is None
        if pending:
            self.sim.emergency_stop()
            with self._emergency_lock:
                request.applied_ns = time.monotonic_ns()

    def _drain_emergency(self):
        while True:
            self._emergency_wakeup.wait()
            self._emergency_wakeup.clear()
            with self.condition:
                if self.closed:
                    return
                try:
                    self._settle_emergency()
                except (OSError, ValueError, RuntimeError) as error:
                    self.phase, self.error = "error", f"Acil durdurma kayda alınamadı. {error}"
                    self._seal_failed_run()
                self.condition.notify_all()

    def submit_external(self, request):
        with self.condition:
            if (self.closed or self.gateway is None or self.phase in ("finished", "error")
                    or request.generation != self.gateway.generation):
                return
            command = request.command
            if self.strategy_definition.id != EXTERNAL_STRATEGY or self.phase not in ("running", "paused"):
                self.gateway.publish(Reply("J", self.market_ns, command.token, reason=-1),
                                     generation=request.generation)
                return
            try:
                if self.clock.speed != "max" and not self.seeking_open and self.pending is not None:
                    horizon = min(self._virtual_time(time.monotonic_ns()), self.pending.event[2] - 1)
                    if horizon > self.sim.exchange_ns:
                        self.sim.advance_through(horizon)
                self.external_count += 1
                self.run_audit.record("external_input", {"intake": self.external_count, "after_sequence": self.sequence,
                    "time_ns": str(self.sim.exchange_ns), "dispatch_cursor": self.sim.scheduled_dispatches,
                    "transport_sequence": request.sequence, "frame_hex": request.frame.hex()})
                submit_command(self.sim, command, self.external_count)
            except (OSError, ValueError, RuntimeError) as error:
                self.phase, self.error = "error", f"Harici OUCH isteği işlenemedi. {error}"
                self.seeking_open, self.actual_speed = False, 0
                self._seal_failed_run()
            self.condition.notify_all()

    def market_events(self, run_id, after, limit):
        with self.condition:
            if self.market_feed is None or self.run_audit.id != run_id:
                raise StrategySelectionConflict("Aktif harici OUCH koşusunun kimliğini kullan.")
            finished = self.phase in ("finished", "error")
            through = None if finished else self._virtual_time(time.monotonic_ns())
            result = self.market_feed.read(after, limit, through)
            result.update(run_id=run_id, finished=finished)
            return result

    def _capture_request(self, body):
        name = body.get("file")
        if not isinstance(name, str) or name not in self.capture_files():
            raise ValueError("Çalışma alanındaki bir PCAP dosyasını seç.")
        host = str(ipaddress.IPv4Address(body.get("host", DEFAULT_HOST)))
        port = integer(body.get("port", DEFAULT_PORT), 1, 65_535, "UDP portu")
        if not self.extractor.is_file():
            raise ValueError("hft_asels_feed bulunamadı. Önce CMake ile Release derlemesi al.")
        return name, host, port

    def _recovery_request(self, body):
        entries = body.get("recoveries", [])
        if not isinstance(entries, list) or len(entries) > 16:
            raise ValueError("En fazla 16 yedek ITCH kanal eşlemesi seç.")
        available = self.capture_files()
        recoveries = {}
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"file", "host", "port", "primary_port"}:
                raise ValueError("Yedek kanal için PCAP, hedef IP ve iki UDP portu gerekli.")
            if not isinstance(item["file"], str) or item["file"] not in available:
                raise ValueError("Çalışma alanındaki bir yedek PCAP dosyasını seç.")
            primary = integer(item["primary_port"], 1, 65_535, "Ana UDP portu")
            if primary in recoveries:
                raise ValueError("Her ana kanal için tek yedek eşlemesi seç.")
            recoveries[primary] = (self.root / item["file"], str(ipaddress.IPv4Address(item["host"])),
                                   integer(item["port"], 1, 65_535, "Yedek UDP portu"))
        return recoveries

    @staticmethod
    def _recovery_flags(recovery):
        if not recovery:
            return []
        path, host, port = recovery
        return ["--recovery-capture", str(path), "--recovery-host", host, "--recovery-port", str(port)]

    def _cancel_catalog(self):
        self.catalog_ticket += 1
        if self.catalog_process and self.catalog_process.poll() is None:
            self.catalog_process.terminate()

    def _list_instruments(self, body):
        file, host, anchor = self._capture_request(body)
        recoveries = self._recovery_request(body)
        if (self.catalog.get("file"), self.catalog.get("host"), self.catalog.get("port")) == (file, host, anchor):
            if self.catalog["phase"] == "loading":
                return
        self._cancel_catalog()
        ticket = self.catalog_ticket
        self.catalog = {"phase": "loading", "file": file, "host": host, "port": anchor, "instruments": []}
        threading.Thread(target=self._discover, args=(ticket, file, host, anchor, recoveries), daemon=True).start()

    def _discover(self, ticket, file, host, anchor, recoveries):
        ports = (anchor, *(port for port in EQUITY_PORTS if port != anchor)) if anchor in EQUITY_PORTS else (anchor,)
        instruments, unavailable = {}, []
        for port in ports:
            with self.condition:
                if self.closed or ticket != self.catalog_ticket:
                    return
                try:
                    process = subprocess.Popen(
                        [str(self.extractor), str(self.root / file), host, str(port), "--list-symbols",
                         *self._recovery_flags(recoveries.get(port))],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except OSError as error:
                    unavailable.append({"port": port, "error": str(error)})
                    continue
                self.catalog_process = process
            try:
                output, stderr = process.communicate(timeout=20)
                if process.returncode:
                    raise ValueError(stderr.strip() or "Bu kanalda pay referansı okunamadı.")
                for entry in json.loads(output):
                    symbol = canonical_symbol(entry["symbol"])
                    instruments.setdefault(symbol, {"symbol": symbol, "book_id": entry["book_id"],
                                                     "host": host, "port": port})
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                unavailable.append({"port": port, "error": "Pay listesi zamanında okunamadı."})
            except (ValueError, KeyError, TypeError) as error:
                unavailable.append({"port": port, "error": str(error)})
            finally:
                with self.condition:
                    if self.catalog_process is process:
                        self.catalog_process = None
        with self.condition:
            if self.closed or ticket != self.catalog_ticket:
                return
            error = None if instruments else next((item["error"] for item in unavailable),
                                                  "Kayıtta uygun bir Pay Piyasası payı bulunamadı.")
            self.catalog = {"phase": "ready" if instruments else "error", "file": file, "host": host,
                            "port": anchor, "instruments": [instruments[key] for key in sorted(instruments)],
                            "unavailable_ports": unavailable, "error": error}
            self.condition.notify_all()

    def _load(self, body):
        name, host, port = self._capture_request(body)
        recoveries = self._recovery_request(body)
        if "symbols" in body:
            symbols = body["symbols"]
            if (not isinstance(symbols, list) or not symbols or any(type(symbol) is not str for symbol in symbols)
                    or len(symbols) != len(set(symbols))):
                raise ValueError("Bir veya birden fazla farklı pay seç.")
            if (self.catalog["phase"] != "ready"
                    or (name, host, port) != (self.catalog["file"], self.catalog["host"], self.catalog["port"])):
                raise ValueError("Önce bu kaydın pay listesini aç.")
            available = {entry["symbol"]: entry["port"] for entry in self.catalog["instruments"]}
            if set(symbols) - available.keys():
                raise ValueError("Seçim, bu PCAP için listelenen paylardan oluşmalı.")
            routes = {symbol: available[symbol] for symbol in symbols}
        else:
            routes = {DEFAULT_SYMBOL: port}
        capture = self.root / name
        groups = {}
        for symbol, channel in routes.items():
            groups.setdefault(channel, []).append(symbol)
        cache_paths = {channel: self.cache_file(capture, host, channel, symbols, recoveries.get(channel))
                       for channel, symbols in groups.items()}
        sim = self._create_simulation(self.strategy_definition, self.strategy_params, self.config, routes)
        self._cancel_preparation()
        ticket = self.ticket
        self.cache_path = None
        self.cache_paths = {}
        self.routes = routes
        self.symbols = tuple(routes)
        self.focus_symbol = self.symbols[0]
        self._reset(sim)
        self.file, self.host, self.port = name, host, port
        self.recoveries, self.coverage, self.capture_identity = recoveries, {}, None
        file_size = capture.stat().st_size
        self.total_bytes, self.bytes_read = file_size * len(groups), 0
        self.prepared_bytes = dict.fromkeys(groups, 0)
        self.phase = "preparing"
        threading.Thread(target=self._prepare, args=(ticket, capture, host, groups, cache_paths, file_size, recoveries),
                         daemon=True).start()

    @staticmethod
    def _coverage_fields(line):
        fields = (part.split("=", 1) for part in line.strip().split("\t")[1:] if "=" in part)
        return {key: int(value) if value.isdecimal() else value for key, value in fields}

    def _prepare_channel(self, ticket, capture, host, port, symbols, cache, file_size, recovery, identities):
        process = None
        sidecar = cache.with_suffix(".coverage.json")
        coverage = {}
        try:
            try:
                if cache.is_file() and sidecar.is_file():
                    stored = json.loads(sidecar.read_bytes())
                    if (stored["inputs"] == identities and stored["tape_sha256"] == file_sha256(cache)
                            and stored["status"] in ("itch_end", "mold_end", "capture_prefix")):
                        coverage = stored
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if not coverage:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self.cache_dir) as directory:
                    pending = Path(directory) / "equities.ndjson"
                    with self.condition:
                        if ticket != self.ticket or self.closed:
                            return
                        process = subprocess.Popen(
                            [str(self.extractor), str(capture), host, str(port), str(pending),
                             "--symbols", ",".join(sorted(symbols)), *self._recovery_flags(recovery)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        self.preparations.add(process)
                    problems = deque(maxlen=4)
                    with process.stderr as messages:
                        for line in messages:
                            if line.startswith("PROGRESS\t"):
                                with self.condition:
                                    if ticket == self.ticket:
                                        self.prepared_bytes[port] = min(file_size, int(line.split("\t")[1]))
                                        self.bytes_read = sum(self.prepared_bytes.values())
                            elif line.startswith(("EXTRACT\t", "COVERAGE\t")):
                                coverage = self._coverage_fields(line)
                            elif line.strip():
                                problems.append(line.strip())
                    code = process.wait()
                    if not code and pending.is_file() and coverage:
                        coverage.update(tape_sha256=file_sha256(pending), inputs=identities)
                    with self.condition:
                        if ticket != self.ticket or self.closed:
                            return
                        if code or not coverage or not pending.is_file() or pending.stat().st_size == 0:
                            detail = " ".join(problems) or f"çıkış kodu {code}"
                            self.coverage[str(port)] = {"status": "error", **coverage}
                            raise ValueError(f"UDP {port} hazırlanamadı. {detail}")
                        pending.replace(cache)
                        metadata = Path(directory) / "coverage.json"
                        metadata.write_text(json.dumps(coverage), encoding="utf-8")
                        metadata.replace(sidecar)
            with self.condition:
                if ticket == self.ticket and not self.closed:
                    self.coverage[str(port)] = coverage
                    self.prepared_bytes[port] = file_size
                    self.bytes_read = sum(self.prepared_bytes.values())
        finally:
            if process is not None:
                with self.condition:
                    self.preparations.discard(process)

    def _prepare(self, ticket, capture, host, groups, cache_paths, file_size, recoveries):
        try:
            def ensure_current():
                if ticket != self.ticket or self.closed:
                    raise OSError("PCAP hazırlığı iptal edildi.")

            archives = {recovery[0] for port, recovery in recoveries.items() if port in groups}
            before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in {capture, *archives}}
            binary = self.extractor.stat()
            extractor_sha = file_sha256(self.extractor, check=ensure_current)
            if before[capture][0] != file_size:
                raise ValueError("PCAP hazırlanırken değişti.")
            identities = {path: {"name": path.name, "size": before[path][0], "sha256": file_sha256(path, check=ensure_current)}
                          for path in sorted(before)}
            with ThreadPoolExecutor(max_workers=len(groups)) as executor:
                futures = [executor.submit(self._prepare_channel, ticket, capture, host, port, symbols,
                                           cache_paths[port], file_size, recoveries.get(port),
                                           {"primary": identities[capture], "extractor_sha256": extractor_sha, "archive": (
                                               {**identities[recoveries[port][0]], "host": recoveries[port][1],
                                                "port": recoveries[port][2]} if port in recoveries else None)})
                           for port, symbols in groups.items()]
                for future in futures:
                    future.result()
            if any((path.stat().st_size, path.stat().st_mtime_ns) != original for path, original in before.items()):
                raise ValueError("PCAP hazırlanırken değişti.")
            current = self.extractor.stat()
            if (current.st_size, current.st_mtime_ns) != (binary.st_size, binary.st_mtime_ns):
                raise ValueError("Çıkarıcı PCAP hazırlanırken değişti.")
            with self.condition:
                if ticket == self.ticket and not self.closed:
                    self.cache_paths = cache_paths
                    self.cache_path = next(iter(cache_paths.values()))
                    self.capture_identity = identities[capture]
                    self.extractor_sha256 = extractor_sha
                    self.bytes_read = self.total_bytes
                    self._reset()
                    self.condition.notify_all()
        except (OSError, ValueError) as error:
            with self.condition:
                if ticket == self.ticket and not self.closed:
                    self.error, self.phase = str(error), "error"
                    self.condition.notify_all()

    def _virtual_time(self, now):
        if self.phase == "running" and self.clock.speed != "max" and not self.seeking_open:
            return max(self.market_ns, self.clock.market_at(now))
        if self.phase == "paused":
            return self.paused_market_ns
        return self.market_ns or (self.pending.event[2] if self.pending else 0)

    def control(self, body):
        if not isinstance(body, dict):
            raise ValueError("Bir kontrol nesnesi gönderilmeli.")
        if body.get("action") == "emergency_stop":
            self.request_emergency(body.get("run_id"))
            with self.condition:
                self._settle_emergency()
                self.condition.notify_all()
            return
        with self.condition:
            action = body.get("action")
            now = time.monotonic_ns()
            if action == "load":
                self._load(body)
            elif action == "list_instruments":
                self._list_instruments(body)
            elif action == "focus_instrument":
                symbol = body.get("symbol")
                if type(symbol) is not str or symbol not in self.routes:
                    raise ValueError("Bu replay için seçilmiş bir paya geç.")
                self.focus_symbol = symbol
            elif action == "risk_shock":
                self.shock_percent = parse_shock_percent(body.get("percent"))
            elif action in ("start", "advance_open"):
                if self.phase not in ("ready", "paused"):
                    raise ValueError("Oynatmak için önce bir PCAP hazırla veya oturumu sıfırla.")
                if action == "advance_open" and self.sim.phase_for(self.focus_symbol) == "P_SUREKLI_ISLEM":
                    raise ValueError(f"{self.focus_symbol} zaten sürekli işlem fazında.")
                if self.run_audit is None:
                    self._begin_run()
                self.clock.restart(self._virtual_time(now), now)
                self.started = self.started or now
                self.seeking_open = action == "advance_open"
                self.open_target = self.focus_symbol if self.seeking_open else None
                self.phase = "running"
            elif action == "pause":
                if self.phase == "running":
                    self.paused_market_ns = self._virtual_time(now)
                    self.phase = "paused"
                    self.actual_speed = 0
                    self.seeking_open = False
                    self.open_target = None
                    self.run_audit.checkpoint()
            elif action == "reset":
                self._cancel_preparation()
                self._reset()
                if not self.cache_path:
                    self.bytes_read = 0
            elif action == "speed":
                speed = body.get("value")
                if speed != "max":
                    speed = integer(speed, 1, 1000, "Hız")
                self.clock.restart(self._virtual_time(now), now, speed)
                self.speed_sample = None
                self.actual_speed = 0
            elif action == "strategy":
                enabled = body.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Strateji açık veya kapalı olmalı.")
                if enabled and self.sim.stop_reason:
                    raise ValueError("Risk durdurması kilitli. Önce replay’i başa al.")
                if self.enabled != enabled:
                    self.sim.set_enabled(enabled)
                    self.enabled = enabled
                    self.strategy_controls.append({"enabled": enabled, "sequence": self.sequence,
                                                   "exchange_ns": str(self.sim.exchange_ns),
                                                   "dispatch_cursor": self.sim.scheduled_dispatches})
                    if self.run_audit is not None:
                        self.run_audit.record("strategy_control", self.strategy_controls[-1])
            elif action == "select_strategy":
                if body.keys() - {"action", "strategy_id", "params", "config"}:
                    raise ValueError("Bilinmeyen strateji seçim alanı.")
                self._select_strategy(body.get("strategy_id"), body.get("params", {}), body.get("config", {}))
            elif action == "config":
                updates = body.get("config")
                if not isinstance(updates, dict):
                    raise ValueError("Bilinmeyen strateji ayarı.")
                params = self.strategy_params
                if self.strategy_definition.id == DEFAULT_STRATEGY:
                    params = params | {key: value for key, value in updates.items() if key in params}
                    updates = {key: value for key, value in updates.items() if key not in params}
                self._select_strategy(self.strategy_definition.id, params, updates)
            else:
                raise ValueError("Bilinmeyen kontrol işlemi.")
            self.condition.notify_all()

    def _replay(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.phase == "running")
                if self.closed:
                    return
                try:
                    for _ in range(128):
                        self._settle_emergency()
                        if self.pending is None:
                            self._seal_run("finished")
                            self.phase, self.actual_speed = "finished", 0
                            self.seeking_open = False
                            break
                        record = self.pending
                        target = max(self.market_ns, record.event[2])
                        delay = 0 if self.seeking_open else self.clock.seconds_until(target, time.monotonic_ns())
                        if delay > 0:
                            due = self.sim.next_due()
                            if due is not None and due < target:
                                delay = self.clock.seconds_until(due, time.monotonic_ns())
                                if not delay:
                                    self.sim.advance_through(due)
                                    continue
                            self.condition.wait(timeout=min(delay, 0.5))
                            break
                        event = record.event
                        event[2] = target
                        self.source_port, self.source_sequence = record.port, record.source_sequence
                        self.sim.apply(event, source=record.port, source_sequence=record.source_sequence)
                        self.sequence, self.market_ns = event[1], target
                        self.events += 1
                        if self.market_feed is not None:
                            self.market_feed.publish(record)
                        self.pending = self.stream.read()
                        self._record_history()
                        if (self.seeking_open and event[0] == "O"
                                and self.sim.phase_for(self.open_target) == "P_SUREKLI_ISLEM"):
                            self.phase, self.actual_speed = "paused", 0
                            self.paused_market_ns, self.seeking_open = target, False
                            self.open_target = None
                            break
                except StrategyError as error:
                    if self.sim.sequence > self.sequence:
                        self.events += 1
                    self.sequence, self.market_ns = self.sim.sequence, self.sim.exchange_ns
                    strategy_id = self.strategy_definition.id
                    self.strategy_error = {"id": strategy_id, "sequence": error.sequence,
                                           "message": error.detail}
                    self.phase, self.error = "error", f"{self.strategy_definition.name} ({strategy_id}) durdu. {error}"
                    self.seeking_open, self.actual_speed = False, 0
                    self._seal_failed_run()
                except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
                    self.phase, self.error = "error", f"Replay durdu. {error}"
                    self.seeking_open = False
                    self._seal_failed_run()
            time.sleep(0)

    def _seal_failed_run(self):
        try:
            self._seal_run("error", self.error)
        except (OSError, ValueError, RuntimeError) as error:
            self.error += f" Koşu kaydı kapatılamadı. {error}"
            self.sim.set_audit_sink(None)
            self.sim.set_reply_sink(None)
            self.sim.emergency_stop("audit_error")
            self.sim.close_strategies()
            if self.gateway is not None and self.strategy_definition.id == EXTERNAL_STRATEGY:
                self.gateway.end_session()

    def _record_history(self):
        if self.market_ns - self.last_history < 60 * NS:
            return
        self.last_history = self.market_ns
        time_label = clock_text(self.market_ns)
        for summary in self.sim.summaries():
            if summary["reference_mid_price"] is not None:
                self.history[summary["symbol"]].append({"time": time_label, "mid_price": summary["reference_mid_price"],
                                                        "total_pnl": summary["total_pnl"]})

    def state(self):
        files = self.capture_files()
        with self.condition:
            now = time.monotonic_ns()
            virtual = self._virtual_time(now)
            if self.pending:
                virtual = min(virtual, max(self.market_ns, self.pending.event[2]))
            if self.phase == "running":
                if self.speed_sample and now - self.speed_sample[0] >= NS // 3:
                    self.actual_speed = round((self.market_ns - self.speed_sample[1]) / (now - self.speed_sample[0]), 2)
                    self.speed_sample = None
                if not self.speed_sample:
                    self.speed_sample = (now, self.market_ns)
            state = self.sim.snapshot(self.focus_symbol)
            journal = []
            for entry in state.get("order_log", [])[-80:]:
                journal.append({**entry, "time": clock_text(entry.get("time_ns")),
                                "status": entry.get("status", entry.get("direction"))})
            strategy = {key: state.get(key) for key in (
                "average_price", "realized_pnl", "unrealized_pnl", "total_pnl", "cash", "mark_price",
                "bid_ahead", "ask_ahead", "fills", "ouch_sent", "ouch_received")}
            strategy.update(mid_price=state["reference_mid_price"],
                            inventory=state.get("position", 0), buy_price=state.get("quote_bid"),
                            sell_price=state.get("quote_ask"))
            selection = {"id": self.strategy_definition.id, "version": self.strategy_definition.version,
                         "params": self.strategy_params.copy()}
            diagnostics = (self.sim.strategy_diagnostics(self.focus_symbol)
                           if self.strategy_definition.id == LEARNING_QUOTE_STRATEGY else None)
            config = self.config.copy()
            if self.strategy_definition.id == DEFAULT_STRATEGY:
                config.update(self.strategy_params)
            with self._emergency_lock:
                stop = self._emergency
                stop_ms = (round((stop.applied_ns - stop.requested_ns) / 1_000_000, 3)
                           if stop and stop.applied_ns is not None else None)
                stop_pending = bool(stop and stop.event.is_set() and stop.applied_ns is None)
            stop_reason = self.sim.stop_reason or ("manual_pending" if stop_pending else None)
            return {"phase": self.phase, "error": self.error, "file": self.file or (files[-1] if files else ""),
                    "files": files, "host": self.host, "port": self.port, "speed": self.clock.speed,
                    "seeking_open": self.seeking_open,
                    "strategy_enabled": self.enabled and not stop_reason,
                    "risk_stop": stop_reason, "emergency_applied_ms": stop_ms,
                    "config": config, "market_phase": state.get("phase"),
                    "coverage": self.coverage,
                    "recovery": [{"primary_port": port, "file": path.name, "host": address, "port": udp}
                                 for port, (path, address, udp) in self.recoveries.items()],
                    "run": self.run_audit.public() if self.run_audit else None,
                    "interrupted_runs": [{"id": item.get("id"), "status": item["status"]}
                                         for item in self.interrupted_runs],
                    "market_feed": self.market_feed is not None,
                    "ouch_gateway": ({"host": self.gateway.address[0], "port": self.gateway.address[1],
                                      "session": self.gateway.session} if self.gateway else None),
                    "strategy_selection": selection, "strategy_error": self.strategy_error,
                    "strategy_diagnostics": diagnostics,
                    "strategy_controls": list(self.strategy_controls),
                    "catalog": self.catalog, "symbols": self.symbols, "focus_symbol": self.focus_symbol,
                    "instruments": [{**item, "source_port": self.routes[item["symbol"]]}
                                    for item in self.sim.summaries()],
                    "risk": self.sim.price_shock(self.shock_percent),
                    "progress": {"events": self.events, "sequence": self.sequence,
                                 "source_port": self.source_port, "source_sequence": self.source_sequence,
                                 "market_time": clock_text(virtual), "actual_speed": self.actual_speed,
                                 "elapsed_seconds": round((now - self.started) / NS, 1) if self.started else 0,
                                 "preparation_percent": round(self.bytes_read * 100 / self.total_bytes, 1) if self.total_bytes else 0,
                                 "processed_bytes": self.bytes_read, "total_bytes": self.total_bytes},
                    "book": {"bids": state.get("bids", []), "asks": state.get("asks", [])},
                    "reference_book": {"bids": state["reference_bids"], "asks": state["reference_asks"]},
                    "matching_audit": state["matching_audit"],
                    "strategy": strategy, "journal": journal, "history": list(self.history[self.focus_symbol])}

    def close(self):
        with self.condition:
            self.closed = True
            self._cancel_preparation()
            self._cancel_catalog()
            if self.stream:
                self.stream.close()
            try:
                self._seal_run("aborted")
            except (OSError, ValueError, RuntimeError) as error:
                self.error = f"Koşu kaydı kapatılamadı. {error}"
            finally:
                self.sim.close_strategies()
                self.condition.notify_all()
        self.worker.join(timeout=2)
        self._emergency_wakeup.set()
        self._emergency_worker.join(timeout=2)
        if self.gateway is not None:
            self.gateway.close()
        close_registry = getattr(self.strategy_registry, "close", None)
        if callable(close_registry):
            close_registry()


def make_server(controller, port):
    static = {"/": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"),
              "/app.js": ("app.js", "text/javascript")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def write_body(self, status, content_type, data):
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def write_json(self, data, status=200):
            self.write_body(status, "application/json", json.dumps(data, ensure_ascii=False, allow_nan=False).encode())

        def local_host(self):
            return self.headers.get("Host", "").lower() in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.local_host():
                self.send_error(403)
            elif self.path == "/api/state":
                self.write_json(controller.state())
            elif self.path == "/api/strategies":
                self.write_json(controller.strategy_catalog())
            elif self.path.startswith("/api/market-feed?"):
                try:
                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True, max_num_fields=3)
                    if (set(params) - {"run_id", "after", "limit"} or "run_id" not in params
                            or any(len(values) != 1 for values in params.values())):
                        raise ValueError("Piyasa akışı için koşu kimliği ve imleç gerekli.")
                    after = integer(int(params.get("after", ["0"])[0]), 0, 2**63 - 1, "İmleç")
                    limit = integer(int(params.get("limit", ["256"])[0]), 1, 1_024, "Olay sayısı")
                    result = controller.market_events(params["run_id"][0], after, limit)
                    self.write_json(result, 409 if result["overrun"] else 200)
                except StrategySelectionConflict as error:
                    self.write_json({"error": str(error)}, 409)
                except ValueError as error:
                    self.write_json({"error": str(error)}, 400)
            elif self.path in static:
                name, content_type = static[self.path]
                self.write_body(200, content_type, (STATIC / name).read_bytes())
            elif match := re.fullmatch(r"/api/runs/([0-9a-f]{32})/(manifest\.json|events\.ndjson)", self.path):
                run_id, name = match.groups()
                path = controller.run_dir / run_id / name
                try:
                    if name == "events.ndjson":
                        manifest = json.loads(path.with_name("manifest.json").read_bytes())
                        if manifest.get("status") in ("running", "interrupted") or not manifest.get("events_sha256"):
                            self.write_json({"error": "Bu günlük doğrulanmış ve kapatılmış bir koşuya ait değil."}, 409)
                            return
                    with path.open("rb") as stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json" if name == "manifest.json"
                                         else "application/x-ndjson")
                        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        shutil.copyfileobj(stream, self.wfile)
                except FileNotFoundError:
                    self.send_error(404)
                except (OSError, ValueError):
                    self.send_error(409)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.local_host():
                self.send_error(403)
                return
            if self.path not in ("/api/control", "/api/emergency-stop"):
                self.send_error(404)
                return
            try:
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}",
                                             f"http://localhost:{self.server.server_port}"}:
                    self.write_json({"error": "Kontrol yalnızca yerel dashboard oturumuna açık."}, 403)
                    return
                length = int(self.headers.get("Content-Length", 0))
                if not 1 <= length <= 8192 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Geçerli bir JSON kontrol isteği gönder.")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/emergency-stop":
                    if not isinstance(body, dict) or body.keys() != {"run_id"} or not isinstance(body["run_id"], str):
                        raise ValueError("Acil durdurma için aktif koşu kimliği gerekli.")
                    self.write_json(controller.request_emergency(body["run_id"]), 202)
                else:
                    controller.control(body)
                    self.write_json(controller.state())
            except StrategySelectionConflict as error:
                self.write_json({"error": str(error)}, 409)
            except (ValueError, OSError) as error:
                self.write_json({"error": str(error)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    from .isolation import load_isolated_registry
    from .ouch_gateway import OuchGateway

    parser = argparse.ArgumentParser(description="Yerel Pay Piyasası replay dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--extractor", type=Path)
    parser.add_argument("--strategy-module", action="append", default=[],
                        help="Bir Docker sanal alanında yüklenecek yerel Python modülü.")
    parser.add_argument("--ouch-port", type=int, help="İsteğe bağlı yerel OUCH dinleyici portu.")
    parser.add_argument("--ouch-user", default="SIM", help="En çok altı karakterlik test hesabı.")
    args = parser.parse_args()
    try:
        if args.ouch_port is not None and not os.getenv("HFT_OUCH_PASSWORD"):
            raise ValueError("OUCH için HFT_OUCH_PASSWORD ortam değişkenini ayarla.")
        registry = load_isolated_registry(args.strategy_module, module_root=ROOT)
        controller = Controller(extractor=args.extractor, strategy_registry=registry)
        if args.ouch_port is not None:
            gateway = OuchGateway(controller.submit_external, port=args.ouch_port, username=args.ouch_user,
                                  password=os.environ["HFT_OUCH_PASSWORD"])
            try:
                gateway.start()
            except BaseException:
                gateway.close()
                controller.close()
                raise
            controller.gateway = gateway
    except (ValueError, OSError, RuntimeError) as error:
        parser.error(str(error))
    try:
        with make_server(controller, args.port) as server:
            print(f"Pay Piyasası dashboard: http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
