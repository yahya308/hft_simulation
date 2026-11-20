"""Example strategy built on the same public API as local strategy modules."""

from ..strategy_api import Cancel, Enter, Replace


class BidOnly:
    def __init__(self, quantity=10, decision_interval_ms=1000):
        self.quantity = quantity
        self.interval_ns = decision_interval_ms * 1_000_000
        self.generation = None
        self.next_decision_ns = 0

    def on_event(self, event, view, updates):
        if self.generation != view.generation:
            self.generation = view.generation
            self.next_decision_ns = 0
        if event.exchange_ns < self.next_decision_ns or "B" in view.pending_sides:
            return ()

        self.next_decision_ns = event.exchange_ns + self.interval_ns
        live = next((order for order in view.orders if order.side == "B"), None)
        price = view.best_bid
        quantity = min(self.quantity, max(0, view.buy_capacity))
        if view.round_lot:
            quantity -= quantity % view.round_lot
        if (not view.round_lot or not quantity or price is None
                or view.best_ask is None or price >= view.best_ask):
            return (Cancel(live.order_id),) if live else ()
        if live is None:
            return (Enter("B", price, quantity),)
        if live.price != price or live.remaining_quantity > quantity:
            return (Replace(live.order_id, price, quantity),)
        return ()
