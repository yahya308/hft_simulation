"""Shared replay settings and deterministic link delays."""

from random import Random
from zlib import crc32


EXTRA_LIMITS = {
    "market_latency_us": (0, 10_000_000),
    "market_jitter_us": (0, 10_000_000),
    "order_jitter_us": (0, 10_000_000),
    "reply_latency_us": (0, 10_000_000),
    "reply_jitter_us": (0, 10_000_000),
    "latency_seed": (0, 2**32 - 1),
    "order_size_limit": (0, 1_000_000),
    "order_rate_limit": (0, 1_000_000),
    "gross_limit_tl": (0, 10_000_000_000),
    "loss_limit_tl": (0, 10_000_000_000),
}
EXTRA_DEFAULTS = dict.fromkeys(EXTRA_LIMITS, 0)


class LinkDelay:
    """Deliver in insertion order even when independent jitter samples overlap."""

    def __init__(self, symbol: str, lane: str, seed: int, base_us: int, jitter_us: int):
        self.base = base_us
        self.jitter = jitter_us
        self.random = Random(seed ^ crc32(f"{symbol}/{lane}".encode("ascii"))) if jitter_us else None
        self.last = 0

    def arrival(self, time_ns: int) -> int:
        offset = self.random.randint(-self.jitter, self.jitter) if self.random is not None else 0
        self.last = max(self.last, time_ns + max(0, self.base + offset) * 1_000)
        return self.last
