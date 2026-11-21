"""Passive two-sided quotes with a causal, one-feature online logistic model."""

from dataclasses import dataclass
from math import exp

from ..strategy_api import Cancel, Enter, MarketEvent, OrderIntent, OrderUpdate, Replace, StrategyView


_CONTINUOUS = frozenset(("P_SUREKLI_ISLEM", "OPEN", "CONTINUOUS", "CONTINUOUS TRADING"))


def _parameters(pair: tuple[float, float]) -> dict[str, float]:
    return {"bias": pair[0], "imbalance": pair[1]}


@dataclass(frozen=True, slots=True)
class _Update:
    sequence: int
    label: int
    forecast: float
    training_imbalance: float
    gradient: tuple[float, float]
    weights_before: tuple[float, float]
    weights_after: tuple[float, float]


class LearningQuote:
    def __init__(self, quantity: int = 10, quote_interval_ms: int = 1000, spread_ticks: int = 0,
                 learning_rate: float = 0.1, signal_threshold: float = 0.55,
                 minimum_samples: int = 20) -> None:
        self.quantity = quantity
        self.interval_ns = quote_interval_ms * 1_000_000
        self.spread_ticks = spread_ticks
        self.learning_rate = learning_rate
        self.signal_threshold = signal_threshold
        self.minimum_samples = minimum_samples
        self._next_quote_ns = 0
        self._generation = -1
        self._weights = (0.0, 0.0)
        self._samples = 0
        self._sample: tuple[int, float, float] | None = None
        self._imbalance: float | None = None
        self._last_update: _Update | None = None
        self._bid_offset = self._ask_offset = spread_ticks

    def _predict(self, imbalance: float) -> float:
        bias, slope = self._weights
        logit = bias + slope * imbalance
        tail = exp(-abs(logit))
        return (1.0 if logit >= 0.0 else tail) / (1.0 + tail)

    def _observe(self, sequence: int, view: StrategyView) -> None:
        bid, ask = view.reference_best_bid, view.reference_best_ask
        bid_quantity, ask_quantity = view.reference_best_bid_quantity, view.reference_best_ask_quantity
        if (bid is None or ask is None or not 0 < bid < ask
                or bid_quantity <= 0 or ask_quantity <= 0):
            self._sample = self._imbalance = None
            return

        midpoint_sum = bid + ask
        imbalance = (bid_quantity - ask_quantity) / (bid_quantity + ask_quantity)
        self._imbalance = imbalance
        if self._sample is not None:
            previous_midpoint, training_imbalance, forecast = self._sample
            if midpoint_sum == previous_midpoint:
                return
            label = int(midpoint_sum > previous_midpoint)
            signed_error = forecast - label
            gradient = (signed_error, signed_error * training_imbalance)
            before = self._weights
            self._weights = (before[0] - self.learning_rate * gradient[0],
                             before[1] - self.learning_rate * gradient[1])
            self._samples += 1
            self._last_update = _Update(sequence, label, forecast, training_imbalance,
                                        gradient, before, self._weights)
        # Save the features and prediction before the next price move is known.
        self._sample = midpoint_sum, imbalance, self._predict(imbalance)

    def on_event(self, event: MarketEvent, view: StrategyView,
                 updates: tuple[OrderUpdate, ...]) -> tuple[OrderIntent, ...]:
        if view.generation != self._generation:
            self._generation = view.generation
            self._sample = self._imbalance = None
            self._next_quote_ns = 0
            self._bid_offset = self._ask_offset = self.spread_ticks
        if view.phase not in _CONTINUOUS:
            self._sample = self._imbalance = None
            return ()

        self._observe(event.sequence, view)
        if event.exchange_ns < self._next_quote_ns:
            return ()
        bid, ask = view.best_bid, view.best_ask
        valid_market = bid is not None and ask is not None and 0 < bid < ask
        if not valid_market and not view.orders:
            return ()
        self._next_quote_ns = event.exchange_ns + self.interval_ns

        self._bid_offset = self._ask_offset = self.spread_ticks
        if self._samples >= self.minimum_samples and self._imbalance is not None:
            probability = self._predict(self._imbalance)
            if probability > 0.5 and probability >= self.signal_threshold:
                self._ask_offset += 1
            elif probability < 0.5 and probability <= 1 - self.signal_threshold:
                self._bid_offset += 1

        intents = []
        for side, best, capacity, offset in (("B", bid, view.buy_capacity, self._bid_offset),
                                              ("S", ask, view.sell_capacity, self._ask_offset)):
            if side in view.pending_sides:
                continue
            live = view.order(side)
            price = view.target_price(side, best, offset) if valid_market else None
            quantity = min(self.quantity, max(0, capacity))
            quantity -= quantity % (view.round_lot or 1)
            if price is None or not quantity or not view.round_lot:
                if live is not None:
                    intents.append(Cancel(live.order_id))
            elif live is None:
                intents.append(Enter(side, price, quantity))
            elif live.price != price or live.remaining_quantity > quantity:
                intents.append(Replace(live.order_id, price, quantity))
        return tuple(intents)

    def diagnostics(self) -> dict:
        last = self._last_update
        return {
            "samples": self._samples,
            "probability_up": self._predict(self._imbalance) if self._imbalance is not None else None,
            "imbalance": self._imbalance,
            "weights": _parameters(self._weights),
            "last_update": None if last is None else {
                "sequence": last.sequence,
                "label": last.label,
                "forecast": last.forecast,
                "training_imbalance": last.training_imbalance,
                "gradient": _parameters(last.gradient),
                "weights_before": _parameters(last.weights_before),
                "weights_after": _parameters(last.weights_after),
            },
            "bid_offset_ticks": self._bid_offset,
            "ask_offset_ticks": self._ask_offset,
        }
