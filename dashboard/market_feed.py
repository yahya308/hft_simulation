"""Bounded loopback view of normalized historical events for OUCH clients."""

from collections import deque
from itertools import islice

from .scenario import LinkDelay


class MarketFeed:
    def __init__(self, config: dict, capacity: int = 32_768):
        self._records = deque(maxlen=capacity)
        self._delay = LinkDelay("EXTERNAL", "market", config["latency_seed"],
                                config["market_latency_us"], config["market_jitter_us"])
        self.published = 0

    def publish(self, record):
        available = self._delay.arrival(record.event[2])
        self.published += 1
        self._records.append((available, {
            "cursor": self.published, "port": record.port,
            "source_sequence": record.source_sequence, "available_time_ns": str(available),
            "event": list(record.event),
        }))

    def read(self, after: int, limit: int, through_ns: int | None) -> dict:
        first = self.published - len(self._records) + 1
        if after > self.published:
            raise ValueError("Piyasa akışı imleci yayımlanan olayların ilerisinde.")
        if after < first - 1:
            return {"first_available": first, "last_published": self.published, "overrun": True}
        events = []
        start = after - first + 1
        for due, record in islice(self._records, start, start + limit):
            if through_ns is not None and due > through_ns:
                break
            events.append(record)
        return {"first_available": first, "last_published": self.published, "overrun": False,
                "next_cursor": events[-1]["cursor"] if events else after, "events": events}
