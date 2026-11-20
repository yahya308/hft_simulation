"""Passive two-sided quoting at a fixed virtual-time cadence."""

from ..strategy_api import Cancel, Enter, MarketEvent, OrderIntent, OrderUpdate, Replace, StrategyView


class TwoSidedQuote:
    def __init__(self, quantity: int = 10, quote_interval_ms: int = 1_000, spread_ticks: int = 0) -> None:
        self.quantity = quantity
        self.interval_ns = quote_interval_ms * 1_000_000
        self.spread_ticks = spread_ticks
        self._next_quote_ns = 0
        self._generation = -1

    def _due(self, exchange_ns: int, generation: int) -> bool:
        return generation != self._generation or exchange_ns >= self._next_quote_ns

    def on_event(self, event: MarketEvent, view: StrategyView,
                 updates: tuple[OrderUpdate, ...]) -> tuple[OrderIntent, ...]:
        if not self._due(event.exchange_ns, view.generation):
            return ()
        if self._generation != view.generation:
            self._generation = view.generation
            self._next_quote_ns = 0
        valid_market = view.best_bid is not None and view.best_ask is not None and view.best_bid < view.best_ask
        if not valid_market and not view.orders:
            return ()
        self._next_quote_ns = event.exchange_ns + self.interval_ns
        intents = []
        for side, best, capacity in (("B", view.best_bid, view.buy_capacity),
                                     ("S", view.best_ask, view.sell_capacity)):
            if side in view.pending_sides:
                continue
            live = view.order(side)
            price = view.target_price(side, best, self.spread_ticks) if valid_market else None
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
