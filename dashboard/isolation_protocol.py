"""Bounded JSON messages shared by the replay process and the strategy worker."""

import json
from dataclasses import asdict

from .strategy_api import Cancel, Enter, MarketEvent, OpenOrder, OrderUpdate, Replace, StrategyView


MAX_FRAME_BYTES = 256 * 1024
MAX_INTENTS = 64
MEMORY_BYTES = 128 * 1024 * 1024
CPU_SECONDS = 120
MAX_STRATEGIES = 32


def encode_frame(value: object) -> bytes:
    data = (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("Strateji mesajı boyut sınırını aşıyor.")
    return data


def _reject_constant(value: str):
    raise ValueError(f"Geçersiz JSON sayısı: {value}.")


def decode_frame(data: bytes):
    if not data or len(data) > MAX_FRAME_BYTES or not data.endswith(b"\n"):
        raise ValueError("Sınırlı strateji mesajı alınamadı.")
    return json.loads(data, parse_constant=_reject_constant)


def event_request(event: MarketEvent, view: StrategyView, updates: tuple[OrderUpdate, ...]) -> dict:
    return {"op": "event", "event": asdict(event), "view": asdict(view),
            "updates": [asdict(update) for update in updates]}


def event_arguments(request: dict) -> tuple[MarketEvent, StrategyView, tuple[OrderUpdate, ...]]:
    event = request["event"]
    view = request["view"]
    event["args"] = tuple(event["args"])
    view["orders"] = tuple(OpenOrder(**order) for order in view["orders"])
    view["pending_sides"] = tuple(view["pending_sides"])
    view["tick_bands"] = tuple(tuple(band) for band in view["tick_bands"])
    return MarketEvent(**event), StrategyView(**view), tuple(OrderUpdate(**record) for record in request["updates"])


_INTENT_TYPES = {"Enter": Enter, "Replace": Replace, "Cancel": Cancel}


def encode_intents(intents: tuple) -> list[dict]:
    if type(intents) is not tuple or len(intents) > MAX_INTENTS:
        raise TypeError("Strateji sınırlı sayıda emir kararı içeren bir demet döndürmeli.")
    result = []
    for intent in intents:
        kind = type(intent).__name__
        if _INTENT_TYPES.get(kind) is not type(intent):
            raise TypeError("Strateji Enter, Replace veya Cancel kararı döndürmeli.")
        result.append({"kind": kind, "fields": asdict(intent)})
    return result


def decode_intents(data: object) -> tuple:
    if type(data) is not list or len(data) > MAX_INTENTS:
        raise ValueError("İzole stratejinin emir kararları geçersiz.")
    intents = []
    for item in data:
        if type(item) is not dict or item.keys() != {"kind", "fields"} or type(item["fields"]) is not dict:
            raise ValueError("İzole strateji kararı geçersiz.")
        if type(item["kind"]) is not str or item["kind"] not in _INTENT_TYPES:
            raise ValueError("İzole strateji emir türü geçersiz.")
        intents.append(_INTENT_TYPES[item["kind"]](**item["fields"]))
    return tuple(intents)
