import argparse
import hashlib
import ipaddress
import json
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .multi_simulation import MultiSimulation
from .portfolio_risk import parse_shock_percent
from .replay_stream import ReplayStream
from .simulation import canonical_symbol
from .strategy_api import StrategyError
from .strategy_registry import DEFAULT_STRATEGY, LEARNING_QUOTE_STRATEGY, load_registry


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
ISTANBUL = timezone(timedelta(hours=3))
NS = 1_000_000_000
DEFAULT_HOST, DEFAULT_PORT = "233.113.216.71", 21001
EQUITY_PORTS = (21001, 21002, 21003, 21004)
DEFAULT_SYMBOL = "ASELS.E"
DEFAULTS = {"position_limit": 100, "latency_us": 100, "fee_bps": 0}
LIMITS = {"position_limit": (1, 1_000_000), "latency_us": (0, 10_000_000), "fee_bps": (0, 1_000)}


def clock_text(value):
    if not value:
        return None
    seconds, fraction = divmod(int(value), NS)
    wall = datetime.fromtimestamp(seconds, ISTANBUL)
    return f"{wall:%H:%M:%S}.{fraction // 1_000_000:03d}"


class StrategySelectionConflict(ValueError):
    pass


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
        self.condition = threading.Condition()
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

    def _simulation(self):
        return self._create_simulation(self.strategy_definition, self.strategy_params, self.config)

    def _create_simulation(self, definition, params, config, routes=None):
        if (definition.id in (DEFAULT_STRATEGY, "bid_only", LEARNING_QUOTE_STRATEGY)
                and params["quantity"] > config["position_limit"]):
            raise ValueError("Emir miktarı pozisyon limitini aşamaz.")
        routes = self.routes if routes is None else routes
        settings = {"max_position": config["position_limit"], "latency_us": config["latency_us"],
                    "fee_bps": config["fee_bps"], "enabled": self.enabled}
        return MultiSimulation({symbol: settings for symbol in routes}, sources=routes,
                               strategy_factory=lambda _symbol: definition.create(params))

    def _select_strategy(self, strategy_id, params, updates):
        if self.phase not in ("idle", "ready"):
            raise StrategySelectionConflict("Strateji veya ayarları değiştirmek için önce Başa al düğmesini kullan.")
        if not isinstance(updates, dict) or updates.keys() - LIMITS.keys():
            raise ValueError("Bilinmeyen ortak simülasyon ayarı.")
        definition = self.strategy_registry.get(strategy_id)
        params = definition.resolve(params)
        config = self.config | {key: integer(value, *LIMITS[key], key) for key, value in updates.items()}
        if (definition.id, params, config) == (self.strategy_definition.id, self.strategy_params, self.config):
            return
        sim = self._create_simulation(definition, params, config)
        self.strategy_definition, self.strategy_params, self.config, self.sim = definition, params, config, sim

    def capture_files(self):
        return sorted(item.name for item in self.root.glob("*.pcap")
                      if item.is_file() and item.resolve().parent == self.root)

    def cache_file(self, capture, host, port, symbols=(DEFAULT_SYMBOL,)):
        stat = capture.stat()
        identity = (capture.name, stat.st_size, stat.st_mtime_ns, host, port,
                    tuple(sorted(symbols)), self.extractor.stat().st_mtime_ns, "equity-itch-v2112-v1")
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

    def _capture_request(self, body):
        name = body.get("file")
        if not isinstance(name, str) or name not in self.capture_files():
            raise ValueError("Çalışma alanındaki bir PCAP dosyasını seç.")
        host = str(ipaddress.IPv4Address(body.get("host", DEFAULT_HOST)))
        port = integer(body.get("port", DEFAULT_PORT), 1, 65_535, "UDP portu")
        if not self.extractor.is_file():
            raise ValueError("hft_asels_feed bulunamadı. Önce CMake ile Release derlemesi al.")
        return name, host, port

    def _cancel_catalog(self):
        self.catalog_ticket += 1
        if self.catalog_process and self.catalog_process.poll() is None:
            self.catalog_process.terminate()

    def _list_instruments(self, body):
        file, host, anchor = self._capture_request(body)
        if (self.catalog.get("file"), self.catalog.get("host"), self.catalog.get("port")) == (file, host, anchor):
            if self.catalog["phase"] == "loading":
                return
        self._cancel_catalog()
        ticket = self.catalog_ticket
        self.catalog = {"phase": "loading", "file": file, "host": host, "port": anchor, "instruments": []}
        threading.Thread(target=self._discover, args=(ticket, file, host, anchor), daemon=True).start()

    def _discover(self, ticket, file, host, anchor):
        ports = (anchor, *(port for port in EQUITY_PORTS if port != anchor)) if anchor in EQUITY_PORTS else (anchor,)
        instruments, unavailable = {}, []
        for port in ports:
            with self.condition:
                if self.closed or ticket != self.catalog_ticket:
                    return
                try:
                    process = subprocess.Popen(
                        [str(self.extractor), str(self.root / file), host, str(port), "--list-symbols"],
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
        cache_paths = {channel: self.cache_file(capture, host, channel, symbols)
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
        file_size = capture.stat().st_size
        self.total_bytes, self.bytes_read = file_size * len(groups), 0
        self.prepared_bytes = dict.fromkeys(groups, 0)
        self.phase = "preparing"
        threading.Thread(target=self._prepare, args=(ticket, capture, host, groups, cache_paths, file_size),
                         daemon=True).start()

    def _prepare_channel(self, ticket, capture, host, port, symbols, cache, file_size):
        process = None
        try:
            if not cache.is_file():
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self.cache_dir) as directory:
                    pending = Path(directory) / "equities.ndjson"
                    with self.condition:
                        if ticket != self.ticket or self.closed:
                            return
                        process = subprocess.Popen(
                            [str(self.extractor), str(capture), host, str(port), str(pending),
                             "--symbols", ",".join(sorted(symbols))],
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
                            elif line.strip() and not line.startswith("EXTRACT\t"):
                                problems.append(line.strip())
                    code = process.wait()
                    with self.condition:
                        if ticket != self.ticket or self.closed:
                            return
                        if code or not pending.is_file() or pending.stat().st_size == 0:
                            detail = " ".join(problems) or f"çıkış kodu {code}"
                            raise ValueError(f"UDP {port} hazırlanamadı. {detail}")
                        pending.replace(cache)
            with self.condition:
                if ticket == self.ticket and not self.closed:
                    self.prepared_bytes[port] = file_size
                    self.bytes_read = sum(self.prepared_bytes.values())
        finally:
            if process is not None:
                with self.condition:
                    self.preparations.discard(process)

    def _prepare(self, ticket, capture, host, groups, cache_paths, file_size):
        try:
            with ThreadPoolExecutor(max_workers=len(groups)) as executor:
                futures = [executor.submit(self._prepare_channel, ticket, capture, host, port, symbols,
                                           cache_paths[port], file_size) for port, symbols in groups.items()]
                for future in futures:
                    future.result()
            with self.condition:
                if ticket == self.ticket and not self.closed:
                    self.cache_paths = cache_paths
                    self.cache_path = next(iter(cache_paths.values()))
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
                if self.enabled != enabled:
                    self.sim.set_enabled(enabled)
                    self.enabled = enabled
                    self.strategy_controls.append({"enabled": enabled, "sequence": self.sequence,
                                                   "exchange_ns": str(self.market_ns)})
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
                        if self.pending is None:
                            self.sim.finish()
                            self.phase, self.actual_speed = "finished", 0
                            self.seeking_open = False
                            break
                        record = self.pending
                        target = max(self.market_ns, record.event[2])
                        delay = 0 if self.seeking_open else self.clock.seconds_until(target, time.monotonic_ns())
                        if delay > 0:
                            self.condition.wait(timeout=min(delay, 0.5))
                            break
                        event = record.event
                        event[2] = target
                        self.source_port, self.source_sequence = record.port, record.source_sequence
                        self.sim.apply(event, source=record.port, source_sequence=record.source_sequence)
                        self.sequence, self.market_ns = event[1], target
                        self.events += 1
                        self.pending = self.stream.read()
                        self._record_history()
                        if (self.seeking_open and event[0] == "O"
                                and self.sim.phase_for(self.open_target) == "P_SUREKLI_ISLEM"):
                            self.phase, self.actual_speed = "paused", 0
                            self.paused_market_ns, self.seeking_open = target, False
                            self.open_target = None
                            break
                except StrategyError as error:
                    self.sequence, self.market_ns = self.sim.sequence, self.sim.exchange_ns
                    self.events += 1
                    strategy_id = self.strategy_definition.id
                    self.strategy_error = {"id": strategy_id, "sequence": error.sequence,
                                           "message": error.detail}
                    self.phase, self.error = "error", f"{self.strategy_definition.name} ({strategy_id}) durdu. {error}"
                    self.seeking_open, self.actual_speed = False, 0
                except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
                    self.phase, self.error = "error", f"Replay durdu. {error}"
                    self.seeking_open = False
            time.sleep(0)

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
            return {"phase": self.phase, "error": self.error, "file": self.file or (files[-1] if files else ""),
                    "files": files, "host": self.host, "port": self.port, "speed": self.clock.speed,
                    "seeking_open": self.seeking_open,
                    "strategy_enabled": self.enabled, "config": config, "market_phase": state.get("phase"),
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
            self.condition.notify_all()
        self.worker.join(timeout=2)


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

        def do_GET(self):
            if self.path == "/api/state":
                self.write_json(controller.state())
            elif self.path == "/api/strategies":
                self.write_json(controller.strategy_registry.catalog())
            elif self.path in static:
                name, content_type = static[self.path]
                self.write_body(200, content_type, (STATIC / name).read_bytes())
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/api/control":
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
                controller.control(json.loads(self.rfile.read(length)))
                self.write_json(controller.state())
            except StrategySelectionConflict as error:
                self.write_json({"error": str(error)}, 409)
            except (ValueError, OSError) as error:
                self.write_json({"error": str(error)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Yerel Pay Piyasası replay dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--extractor", type=Path)
    parser.add_argument("--strategy-module", action="append", default=[],
                        help="Kayıt tanımları sunan güvenilir yerel Python modülü. Birden çok kez kullanılabilir.")
    args = parser.parse_args()
    try:
        registry = load_registry(args.strategy_module)
        controller = Controller(extractor=args.extractor, strategy_registry=registry)
    except ValueError as error:
        parser.error(str(error))
    try:
        with make_server(controller, args.port) as server:
            print(f"Pay Piyasası dashboard: http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
