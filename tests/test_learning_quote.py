import unittest
from dataclasses import replace
from math import exp

from dashboard.strategies.learning_quote import LearningQuote
from dashboard.strategy_api import Enter, MarketEvent, StrategyView


def observation(sequence, *, ask=10100, generation=0):
    time_ns = sequence * 1_000_000
    event = MarketEvent("A", sequence, time_ns, ())
    view = StrategyView(
        symbol="ASELS.E", phase="P_SUREKLI_ISLEM", exchange_ns=time_ns, sequence=sequence,
        best_bid=10000, best_ask=ask, best_bid_quantity=1, best_ask_quantity=9,
        price_decimals=2, round_lot=1, position=0, buy_capacity=100, sell_capacity=100,
        orders=(), pending_sides=(), generation=generation, tick_bands=((0, 0, 100),),
        reference_best_bid=10000, reference_best_ask=ask,
        reference_best_bid_quantity=800, reference_best_ask_quantity=200,
    )
    return event, view


class LearningQuoteTest(unittest.TestCase):
    def test_up_label_applies_chain_rule_before_widening_ask(self):
        strategy = LearningQuote(quote_interval_ms=1, signal_threshold=0.51, minimum_samples=1)
        first_event, first_view = observation(1)
        self.assertEqual(strategy.on_event(first_event, first_view, ()),
                         (Enter("B", 10000, 10), Enter("S", 10100, 10)))
        self.assertEqual(strategy.diagnostics()["samples"], 0)
        event, view = observation(2)
        strategy.on_event(event, view, ())
        self.assertEqual(strategy.diagnostics()["samples"], 0)

        event, view = observation(3, ask=10200)
        self.assertEqual(strategy.on_event(event, view, ()),
                         (Enter("B", 10000, 10), Enter("S", 10300, 10)))
        details = strategy.diagnostics()
        self.assertEqual(details["samples"], 1)
        update = details["last_update"]
        self.assertEqual((update["sequence"], update["label"], update["forecast"]), (3, 1, 0.5))
        self.assertAlmostEqual(update["training_imbalance"], 0.6)
        self.assertAlmostEqual(update["gradient"]["bias"], -0.5)
        self.assertAlmostEqual(update["gradient"]["imbalance"], -0.3)
        self.assertAlmostEqual(details["weights"]["bias"], 0.05)
        self.assertAlmostEqual(details["weights"]["imbalance"], 0.03)
        self.assertAlmostEqual(details["probability_up"], 1 / (1 + exp(-0.068)))
        self.assertEqual((details["bid_offset_ticks"], details["ask_offset_ticks"]), (0, 1))

    def test_down_label_widens_bid_and_preserves_pending_side_and_lot_cap(self):
        strategy = LearningQuote(quote_interval_ms=1, signal_threshold=0.51, minimum_samples=1)
        event, view = observation(1, ask=10200)
        strategy.on_event(event, view, ())
        event, view = observation(2)
        view = replace(view, round_lot=5, buy_capacity=9, pending_sides=("S",))
        self.assertEqual(strategy.on_event(event, view, ()), (Enter("B", 9900, 5),))
        details = strategy.diagnostics()
        self.assertEqual(details["last_update"]["label"], 0)
        self.assertAlmostEqual(details["weights"]["imbalance"], -0.03)
        self.assertEqual((details["bid_offset_ticks"], details["ask_offset_ticks"]), (1, 0))

    def test_invalidation_drops_pending_observation_but_keeps_learned_weights(self):
        strategy = LearningQuote(quote_interval_ms=1)
        event, view = observation(1)
        strategy.on_event(event, view, ())
        event, view = observation(2, ask=10200)
        strategy.on_event(event, view, ())
        self.assertEqual(strategy.diagnostics()["samples"], 1)
        weights = strategy.diagnostics()["weights"]
        event, view = observation(3, generation=1)
        strategy.on_event(event, view, ())
        self.assertEqual(strategy.diagnostics()["samples"], 1)
        self.assertEqual(strategy.diagnostics()["weights"], weights)

        event, view = observation(4, generation=1)
        missing_book = replace(view, reference_best_ask=None, reference_best_ask_quantity=0)
        strategy.on_event(event, missing_book, ())
        event, view = observation(5, ask=10200, generation=1)
        strategy.on_event(event, view, ())
        self.assertEqual(strategy.diagnostics()["samples"], 1)


if __name__ == "__main__":
    unittest.main()
