"""Read-only replay inputs and passive limit decisions for local ASELS strategies."""

from dataclasses import dataclass
from typing import Literal, Protocol


API_VERSION = 1
Side = Literal["B", "S"]
TickBand = tuple[int, int, int]


def tick_band(price: int, bands: tuple[TickBand, ...]) -> tuple[int, int] | None:
    band = None
    for low, high, step in bands:
        if low <= price and (high == 0 or price <= high) and (band is None or low > band[0]):
            band = low, step
    return band


def valid_price(price: int, bands: tuple[TickBand, ...]) -> bool:
    if not 0 < price < 2**31:
        return False
    band = tick_band(price, bands)
    return band is not None and (price - max(0, band[0])) % band[1] == 0


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One applied ITCH event. Prices in args use the symbol's integer price scale."""

    kind: str
    sequence: int
    exchange_ns: int
    args: tuple[int | str, ...]
    source_port: int | None = None
    source_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """An acknowledged local order. order_id is stable across replacements."""

    order_id: int
    side: Side
    price: int
    remaining_quantity: int
    executed_quantity: int


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Ordered OUCH reply (A, J, U, C, E) from the simulator."""

    kind: str
    sequence: int
    exchange_ns: int
    side: Side | None
    order_id: int | None
    request_sequence: int | None
    token: str
    price: int | None
    quantity: int | None
    reason: int | None


@dataclass(frozen=True, slots=True)
class StrategyView:
    """Instrument, shadow and historical best prices, and the strategy's own state."""

    symbol: str
    phase: str
    exchange_ns: int
    sequence: int
    best_bid: int | None
    best_ask: int | None
    best_bid_quantity: int
    best_ask_quantity: int
    price_decimals: int
    round_lot: int
    position: int
    buy_capacity: int
    sell_capacity: int
    orders: tuple[OpenOrder, ...]
    pending_sides: tuple[Side, ...]
    generation: int
    tick_bands: tuple[TickBand, ...]
    reference_best_bid: int | None = None
    reference_best_ask: int | None = None
    reference_best_bid_quantity: int = 0
    reference_best_ask_quantity: int = 0

    def order(self, side: Side) -> OpenOrder | None:
        return next((order for order in self.orders if order.side == side), None)

    def is_valid_price(self, price: int) -> bool:
        return valid_price(price, self.tick_bands)

    def target_price(self, side: Side, best: int, offset: int = 0) -> int | None:
        """Walk away from the opposite side by offset ticks, crossing tick bands."""
        if side not in ("B", "S") or type(offset) is not int or offset < 0:
            raise ValueError("Taraf B veya S, fiyat uzaklığı negatif olmayan bir tam sayı olmalı.")
        price = best
        direction = -1 if side == "B" else 1
        for _ in range(offset):
            band = tick_band(price + direction, self.tick_bands)
            if band is None:
                return None
            origin, tick = max(0, band[0]), band[1]
            adjacent = price + direction - origin
            index = adjacent // tick if direction < 0 else -(-adjacent // tick)
            price = origin + index * tick
        return price if self.is_valid_price(price) else None


@dataclass(frozen=True, slots=True)
class Enter:
    side: Side
    price: int
    quantity: int


@dataclass(frozen=True, slots=True)
class Replace:
    order_id: int
    price: int
    remaining_quantity: int


@dataclass(frozen=True, slots=True)
class Cancel:
    order_id: int


OrderIntent = Enter | Replace | Cancel


class Strategy(Protocol):
    def on_event(self, event: MarketEvent, view: StrategyView,
                 updates: tuple[OrderUpdate, ...]) -> tuple[OrderIntent, ...]: ...


class StrategyError(RuntimeError):
    """The current decision failed and the engine closed the simulated orders."""

    def __init__(self, sequence: int, detail: str) -> None:
        self.sequence = sequence
        self.detail = detail
        super().__init__(f"ITCH sıra {sequence}: {detail}")
