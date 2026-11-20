"""Merge validated ITCH streams while preserving every source's Mold order."""

from contextlib import ExitStack
from dataclasses import dataclass
from heapq import merge
from itertools import count
import json


@dataclass(slots=True)
class ReplayRecord:
    event: list
    port: int
    source_sequence: int


class ReplayStream:
    def __init__(self, cache_paths):
        self.files = ExitStack()
        try:
            channels = [self._channel(port, self.files.enter_context(path.open(encoding="utf-8")))
                        for port, path in sorted(cache_paths.items())]
            self.records = self._records(channels)
        except BaseException:
            self.files.close()
            raise

    def close(self):
        self.files.close()

    @staticmethod
    def _channel(port, stream):
        previous_ns = previous_sequence = 0
        for line in stream:
            event = json.loads(line)
            sequence, exchange_ns = event[1:3]
            if sequence <= previous_sequence:
                raise ValueError(f"UDP {port} için ITCH sırası artmıyor.")
            previous_ns, previous_sequence = max(previous_ns, exchange_ns), sequence
            event[2] = previous_ns
            yield previous_ns, port, sequence, event
        if previous_sequence == 0:
            raise ValueError(f"UDP {port} olay dosyası boş.")

    @staticmethod
    def _records(channels):
        merged = len(channels) > 1
        order = count(1)
        for _, port, sequence, event in merge(*channels):
            if merged:
                event[1] = next(order)
            yield ReplayRecord(event, port, sequence)

    def read(self):
        return next(self.records, None)
