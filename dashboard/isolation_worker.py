"""Container entry point. The parent starts this only inside the restricted runtime."""

import os
import sys
from importlib import import_module
from pathlib import Path


if __name__ == "__main__":
    # Isolated Python removes the script directory from sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.isolation_protocol import (CPU_SECONDS, MAX_FRAME_BYTES, MAX_STRATEGIES, MEMORY_BYTES,
                                          decode_frame, encode_frame, encode_intents, event_arguments)
from dashboard.strategy_registry import StrategyRegistry


def _verify_limits() -> None:
    import resource

    mounts = Path("/sys/fs/cgroup")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    interfaces = {line.split(":", 1)[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]}
    quota, period = (mounts / "cpu.max").read_text().split()
    cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    if (os.getuid() == 0 or int(status["CapEff"].strip(), 16) != 0 or status["NoNewPrivs"].strip() != "1"
            or status["Seccomp"].strip() != "2"
            or interfaces - {"lo"}
            or quota == "max" or 2 * int(quota) > int(period)
            or not 0 < int((mounts / "memory.max").read_text()) <= MEMORY_BYTES
            or int((mounts / "memory.swap.max").read_text()) != 0
            or int((mounts / "pids.max").read_text()) != 1
            or not 0 < cpu_soft <= cpu_hard <= CPU_SECONDS):
        raise ValueError("Strateji kapsayıcısının kaynak sınırları doğrulanamadı.")


def _load_modules(names: list[str]) -> StrategyRegistry:
    definitions = []
    for name in names:
        for definition in import_module(name).get_strategies():
            definitions.append(definition)
            if len(definitions) > MAX_STRATEGIES:
                raise ValueError("Harici strateji sayısı sınırı aşıldı.")
    return StrategyRegistry(definitions)


def _serve(stream, reply, plugin_root: Path | None = None) -> None:
    _verify_limits()
    if plugin_root is not None:
        # Trusted imports and sandbox verification complete before plugin paths become importable.
        sys.path.append(str(plugin_root))
    registry = instance = None
    while (line := stream.readline(MAX_FRAME_BYTES + 1)):
        request = decode_frame(line)
        operation = request["op"]
        if operation in ("catalog", "init") and registry is None:
            registry = _load_modules(request["modules"])
            if operation == "catalog":
                value = registry.catalog()["strategies"]
            else:
                definition = registry.get(request["id"])
                instance = definition.create(definition.resolve(request["params"]))
                value = None
        elif operation == "event" and instance is not None:
            value = encode_intents(instance.on_event(*event_arguments(request)))
        else:
            raise ValueError("Beklenmeyen strateji protokolü isteği.")
        reply.write(encode_frame({"ok": value}))
        reply.flush()


def main() -> None:
    # Keep user prints outside the protocol, including writes to file descriptor 1.
    with os.fdopen(os.dup(1), "wb", buffering=0) as reply:
        os.dup2(2, 1)
        try:
            _serve(sys.stdin.buffer, reply, Path(__file__).resolve().parents[1] / "plugins")
        except Exception as error:
            message = str(error).strip() or type(error).__name__
            reply.write(encode_frame({"error": message[:512]}))


if __name__ == "__main__":
    main()
