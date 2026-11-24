"""Run external strategies in a local Linux container with a bounded JSON channel."""

from collections.abc import Iterable
from concurrent.futures import Future, TimeoutError
from functools import partial
from hashlib import sha256
from pathlib import Path
from queue import Queue
from re import ASCII, fullmatch
from tempfile import TemporaryDirectory
from threading import Thread
from types import MappingProxyType
from uuid import uuid4
from weakref import WeakSet, finalize
import os
import shutil
import subprocess
import sys

from .isolation_protocol import (CPU_SECONDS, MAX_FRAME_BYTES, MAX_STRATEGIES, MEMORY_BYTES,
                                 decode_frame, decode_intents, encode_frame, event_request)
from .strategy_registry import Parameter, StrategyDefinition, StrategyRegistry, load_registry


STRATEGY_IMAGE = "python:3.12-slim"
STARTUP_TIMEOUT = 15
EVENT_TIMEOUT = 2
CLEANUP_TIMEOUT = 3
MAX_SOURCE_FILES = 256
MAX_SOURCE_BYTES = 4 * 1024 * 1024


class IsolationUnavailable(ValueError):
    """External strategy isolation could not start or validate its OS boundary."""


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}


def _docker_run(command: tuple[str, ...], *args: str, timeout: int = STARTUP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([*command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=_environment(), timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _docker_client(config: Path, module_root: Path) -> tuple[tuple[str, ...], str]:
    docker = shutil.which("docker")
    binary = None if docker is None else Path(docker).resolve()
    if binary is None or any(binary.is_relative_to(root) for root in (module_root, Path.cwd().resolve())):
        raise IsolationUnavailable("Harici stratejiler için yerel Linux Docker motoru ve Docker CLI gerekli.")
    if sys.platform == "win32":
        endpoints = ("npipe:////./pipe/dockerDesktopLinuxEngine", "npipe:////./pipe/docker_engine")
    elif sys.platform.startswith("linux"):
        endpoints = (f"unix:///run/user/{os.getuid()}/docker.sock", "unix:///var/run/docker.sock")
    else:
        raise IsolationUnavailable("Harici strateji izolasyonu Linux ve Windows Docker Linux motorunu destekler.")
    for endpoint in endpoints:
        command = (str(binary), "--config", str(config), "--host", endpoint)
        try:
            info = _docker_run(command, "info", "--format", "{{.OSType}}", timeout=CLEANUP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if info.returncode == 0 and info.stdout.strip() == b"linux":
            try:
                image = _docker_run(command, "image", "inspect", "--format", "{{.Id}}", STRATEGY_IMAGE,
                                    timeout=CLEANUP_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IsolationUnavailable("Yerel strateji imajı doğrulanamadı.") from error
            image_id = image.stdout.decode("ascii", errors="replace").strip()
            if image.returncode != 0 or not fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise IsolationUnavailable(f"Güvenilen {STRATEGY_IMAGE} imajı yerel Docker önbelleğinde gerekli.")
            return command, image_id
    raise IsolationUnavailable("Yerel Docker Linux motoruna erişilemedi.")


def _copy_strategy_sources(module_names: tuple[str, ...], module_root: Path, destination: Path) -> str:
    trusted = Path(__file__).parent
    runtime = destination / "dashboard"
    runtime.mkdir()
    workers = ("__init__.py", "strategy_api.py", "strategy_registry.py", "isolation_protocol.py", "isolation_worker.py")
    for file in workers:
        shutil.copyfile(trusted / file, runtime / file)
    strategies = runtime / "strategies"
    strategies.mkdir()
    for file in (trusted / "strategies").glob("*.py"):
        shutil.copyfile(file, strategies / file.name)

    plugins = destination / "plugins"
    plugins.mkdir()

    copied: set[Path] = set()
    copied_bytes = 0
    for name in module_names:
        if (type(name) is not str or not fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name, flags=ASCII)
                or name.split(".")[0].lower() == "dashboard"):
            raise ValueError("Harici strateji modülü için geçerli bir Python modül adı kullan.")
        target = module_root.joinpath(*name.split("."))
        if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
            raise ValueError(f"Harici strateji modülü çalışma alanında bulunamadı: {name}.")
        top = module_root / name.split(".")[0]
        package = (top / "__init__.py").is_file() or "." in name
        files = top.rglob("*.py") if package else (top.with_suffix(".py"),)
        for file in files:
            if file in copied:
                continue
            if not file.resolve().is_relative_to(module_root):
                raise ValueError("Harici strateji kaynakları çalışma alanı içinde olmalı.")
            copied_bytes += file.stat().st_size
            if len(copied) >= MAX_SOURCE_FILES or copied_bytes > MAX_SOURCE_BYTES:
                raise ValueError("Harici strateji kaynak boyutu sınırı aşıldı.")
            relative = file.relative_to(module_root)
            output = plugins / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, output)
            copied.add(file)

    digest = sha256()
    for file in sorted(copied):
        relative = file.relative_to(module_root)
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(sha256((plugins / relative).read_bytes()).digest())
    return digest.hexdigest()


def _exchange(process: subprocess.Popen, inbox: Queue) -> None:
    while (request := inbox.get()) is not None:
        message, reply = request
        try:
            process.stdin.write(message)
            process.stdin.flush()
            reply.set_result(decode_frame(process.stdout.readline(MAX_FRAME_BYTES + 1)))
        except Exception as error:
            reply.set_exception(error)
            return


def _remove_container(container: str, command: tuple[str, ...]) -> None:
    try:
        _docker_run(command, "rm", "--force", container, timeout=CLEANUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        # Local requests still fail closed if the daemon is unavailable during cleanup.
        pass


def _close_worker(process: subprocess.Popen, inbox: Queue, container: str, command: tuple[str, ...]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    _remove_container(container, command)
    try:
        process.wait(timeout=CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return
    if inbox.empty():
        inbox.put_nowait(None)


class _Worker:
    def __init__(self, command: tuple[str, ...], image: str, root: Path) -> None:
        name = "hft-strategy-" + uuid4().hex
        # The mount syntax separates fields with commas. The staging directory contains only
        # copied strategy sources and the worker protocol, with private ancestors on the host.
        if "," in str(root):
            raise IsolationUnavailable("Strateji geçici dizin yolunda virgül kullanılamıyor.")
        mount = f"type=bind,source={root},target=/strategy,readonly"
        self.inbox: Queue = Queue(maxsize=1)
        try:
            created = _docker_run(
                command, "create", "--name", name, "--rm", "--interactive", "--pull=never",
                "--network=none", "--ipc=none", "--cgroupns=private", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--user=65534:65534", "--no-healthcheck", "--log-driver=none",
                "--init=false", "--cpus=0.5", f"--memory={MEMORY_BYTES}", f"--memory-swap={MEMORY_BYTES}", "--pids-limit=1",
                f"--ulimit=cpu={CPU_SECONDS}:{CPU_SECONDS}", "--ulimit=core=0:0", "--mount", mount,
                "--workdir=/strategy", "--entrypoint=python", image,
                "-I", "-B", "-S", "-X", "utf8", "/strategy/dashboard/isolation_worker.py",
            )
            if created.returncode != 0:
                raise IsolationUnavailable("Yerel Docker güvenli strateji kapsayıcısını oluşturamadı.")
            process = subprocess.Popen([*command, "start", "--attach", "--interactive", name],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired, IsolationUnavailable) as error:
            _remove_container(name, command)
            raise IsolationUnavailable(str(error)) from error
        self._finalizer = finalize(self, _close_worker, process, self.inbox, name, command)
        Thread(target=_exchange, args=(process, self.inbox), daemon=True).start()

    def request(self, message: dict, timeout: int):
        if not self._finalizer.alive:
            raise RuntimeError("İzole strateji çalıştırıcısı kapalı.")
        result: Future = Future()
        try:
            self.inbox.put_nowait((encode_frame(message), result))
            reply = result.result(timeout=timeout)
            if type(reply) is dict and reply.keys() == {"ok"}:
                return reply["ok"]
            if (type(reply) is dict and reply.keys() == {"error"} and type(reply["error"]) is str
                    and len(reply["error"]) <= 512):
                raise RuntimeError(reply["error"])
            raise ValueError("İzole strateji yanıtı geçersiz.")
        except TimeoutError as error:
            self.close()
            raise RuntimeError("İzole stratejinin yanıt süresi doldu.") from error
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._finalizer()


class _Sandbox:
    def __init__(self, module_names: tuple[str, ...], module_root: Path) -> None:
        self.names = module_names
        self.directory = TemporaryDirectory(prefix="hft-strategy-")
        self.root = Path(self.directory.name) / "sources"
        self.workers: WeakSet = WeakSet()
        try:
            config = self.root.with_name("client")
            config.mkdir()
            # An empty CLI config prevents host proxy settings and credentials entering the worker.
            self.command, self.image = _docker_client(config, module_root)
            self.root.mkdir(mode=0o755)
            self.root.chmod(0o755)
            self.source_digest = _copy_strategy_sources(module_names, module_root, self.root)
        except Exception:
            self.directory.cleanup()
            raise

    def worker(self) -> _Worker:
        worker = _Worker(self.command, self.image, self.root)
        self.workers.add(worker)
        return worker

    def discover(self) -> list[dict]:
        worker = self.worker()
        try:
            value = worker.request({"op": "catalog", "modules": self.names}, STARTUP_TIMEOUT)
            if type(value) is not list or len(value) > MAX_STRATEGIES:
                raise ValueError("Harici strateji kataloğu geçersiz.")
            return value
        except (ValueError, RuntimeError, OSError) as error:
            raise IsolationUnavailable(f"Harici strateji kataloğu izolasyonda yüklenemedi. {error}") from error
        finally:
            worker.close()

    def close(self) -> None:
        for worker in tuple(self.workers):
            worker.close()
        self.directory.cleanup()


class _IsolatedStrategy:
    def __init__(self, sandbox: _Sandbox, strategy_id: str, parameters: dict) -> None:
        self.sandbox = sandbox
        self.strategy_id = strategy_id
        self.parameters = parameters
        self.worker: _Worker | None = None
        self.closed = False

    def on_event(self, event, view, updates) -> tuple:
        if self.closed:
            raise RuntimeError("İzole strateji örneği kapalı.")
        try:
            if self.worker is None:
                self.worker = self.sandbox.worker()
                self.worker.request({"op": "init", "modules": self.sandbox.names, "id": self.strategy_id,
                                     "params": self.parameters}, STARTUP_TIMEOUT)
            return decode_intents(self.worker.request(event_request(event, view, updates), EVENT_TIMEOUT))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.closed = True
        if self.worker is not None:
            self.worker.close()
            self.worker = None


def _isolated_factory(sandbox: _Sandbox, strategy_id: str, /, **parameters) -> _IsolatedStrategy:
    return _IsolatedStrategy(sandbox, strategy_id, parameters)


class IsolatedStrategyRegistry(StrategyRegistry):
    def __init__(self, definitions: Iterable[StrategyDefinition], sandbox: _Sandbox, external_ids: list[str]) -> None:
        super().__init__(definitions)
        self._sandbox = sandbox
        self.runtime_image = sandbox.image
        self.fingerprints = MappingProxyType({strategy_id: sandbox.source_digest for strategy_id in external_ids})

    def close(self) -> None:
        self._sandbox.close()


def load_isolated_registry(module_names: Iterable[str] = (), *, module_root: Path | None = None) -> StrategyRegistry:
    builtins = load_registry()
    names = tuple(module_names)
    if not names:
        return builtins
    sandbox = _Sandbox(names, (module_root or Path.cwd()).resolve())
    try:
        definitions = [builtins.get(item["id"]) for item in builtins.catalog()["strategies"]]
        external_ids = []
        for entry in sandbox.discover():
            data = dict(entry)
            parameters = {name: Parameter(**spec) for name, spec in data.pop("parameters").items()}
            factory = partial(_isolated_factory, sandbox, data["id"])
            definitions.append(StrategyDefinition(**data, parameters=parameters, factory=factory))
            external_ids.append(data["id"])
        return IsolatedStrategyRegistry(definitions, sandbox, external_ids)
    except (KeyError, TypeError, AttributeError) as error:
        sandbox.close()
        raise ValueError("İzole strateji kataloğu geçersiz.") from error
    except Exception:
        sandbox.close()
        raise
