"""Load with python -B -m dashboard --strategy-module examples.asels_alis."""

from dashboard.strategy_api import API_VERSION, Cancel, Enter, Replace
from dashboard.strategy_registry import Parameter, StrategyDefinition


class BidImbalance:
    def __init__(self, quantity, bid_ratio_percent):
        self.quantity = quantity
        self.bid_ratio_percent = bid_ratio_percent

    def on_event(self, event, view, updates):
        if "B" in view.pending_sides:
            return ()

        live = view.order("B")
        price = view.best_bid
        quantity = min(self.quantity, view.buy_capacity)
        if view.round_lot:
            quantity -= quantity % view.round_lot
        if (not view.round_lot or not quantity or price is None
                or view.best_ask is None or price >= view.best_ask
                or view.best_bid_quantity * 100 < view.best_ask_quantity * self.bid_ratio_percent):
            return (Cancel(live.order_id),) if live else ()
        if live is None:
            return (Enter("B", price, quantity),)
        if live.price != price or live.remaining_quantity > quantity:
            return (Replace(live.order_id, price, quantity),)
        return ()


def get_strategies():
    return (StrategyDefinition(
        id="bid_imbalance",
        version="1.0.0",
        api_version=API_VERSION,
        name="Alış derinliği örneği",
        description="En iyi alış kademesinin miktarı belirlenen orana ulaşınca pasif alış emri verir.",
        parameters={
            "quantity": Parameter("integer", "Emir miktarı", "Bir alış emrindeki hedef pay adedi.",
                                  10, 1, 100_000, "adet"),
            "bid_ratio_percent": Parameter("integer", "Alış/satış miktar oranı",
                                           "Örneğin %120, en iyi alış miktarının en iyi satışın en az 1,2 katı olmasıdır.",
                                           120, 100, 1_000, "%"),
        },
        factory=BidImbalance,
    ),)
