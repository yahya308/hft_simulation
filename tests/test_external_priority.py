import unittest
from threading import Event
from unittest.mock import patch

from dashboard.ouch import Command
from dashboard.scenario import LinkDelay
from dashboard.simulation import Simulation
from dashboard.strategy_api import Enter
from test_enterprise_runtime import ASELS, THYAO, Tape, ns


class ExternalPriorityTests(unittest.TestCase):
    def test_same_time_intake_order_wins_shared_gross_headroom(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                audit = []
                tape = Tape(symbols=(ASELS, THYAO), external=True, gross_limit_tl=400,
                            audit=lambda kind, data: audit.append((kind, data)))
                tape.start()
                first = Command("O", "FIRST", 8, "B", 300_100, 1)
                second = Command("O", "SECOND", 7, "B", 100_100, 2)
                tape.sim.external_order(first, **({"sequence": 41} if explicit else {}))
                tape.sim.external_order(second, **({"sequence": 42} if explicit else {}))
                tape.through(2)
                self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies],
                                 [("A", "FIRST", 0), ("J", "SECOND", -8)])
                arrivals = [data["external_sequence"] for kind, data in audit if kind == "order_arrival"]
                self.assertEqual(arrivals, [41, 42] if explicit else [1, 2])

    def test_external_order_arrival_fifo_under_different_symbol_jitter(self):
        seed = 0
        slow = LinkDelay(ASELS, "order", seed, 10, 8).arrival(ns(2))
        fast = LinkDelay(THYAO, "order", seed, 10, 8).arrival(ns(2))
        self.assertLess(fast, slow)
        tape = Tape(symbols=(ASELS, THYAO), external=True, latency_us=10, order_jitter_us=8, latency_seed=seed)
        tape.start()
        tape.enter("SLOW", ASELS)
        tape.enter("FAST", THYAO)
        self.assertEqual(tape.sim.next_due(), slow)
        tape.sim.advance_through(slow - 1)
        self.assertEqual(tape.replies, [])
        tape.sim.advance_through(slow)
        self.assertEqual([(r.token, r.time_ns) for r in tape.replies], [("SLOW", slow), ("FAST", slow)])

    def test_itch_wins_external_arrival_time_tie(self):
        tape = Tape(external=True, latency_us=5)
        tape.start()
        tape.enter("TIED")
        tape.event("O", 7, 7, "P_ESLESTIRME")
        self.assertEqual(tape.replies, [])
        tape.through(7)
        self.assertEqual([(r.kind, r.token) for r in tape.replies], [("J", "TIED")])
        self.assertIsNone(tape.sim.snapshot()["quote_bid"])

    def test_regressing_explicit_intake_is_rejected_without_consuming_token(self):
        tape = Tape(external=True)
        tape.start()
        tape.sim.external_order(Command("O", "FIRST", 7, "B", 100_100, 1), sequence=41)
        second = Command("O", "SECOND", 7, "S", 100_900, 1)
        with self.assertRaises(ValueError):
            tape.sim.external_order(second, sequence=41)
        tape.sim.external_order(second, sequence=42)
        tape.through(2)
        self.assertEqual([(r.kind, r.token) for r in tape.replies], [("A", "FIRST"), ("A", "SECOND")])


class EmergencyEventTests(unittest.TestCase):
    def test_event_set_during_order_validation_prevents_commit(self):
        stop = Event()
        tape = Tape(external=True, gross_limit_tl=400)
        tape.sim.set_emergency_event(stop)
        tape.start()
        tape.enter("PENDING")

        def approve(*args):
            stop.set()
            return True

        with patch.object(tape.sim, "allows", side_effect=approve):
            tape.through(2)
        self.assertEqual(tape.sim.stop_reason, "manual")
        self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies], [("J", "PENDING", -9)])
        self.assertIsNone(tape.sim.snapshot()["quote_bid"])

    def test_event_set_during_fill_stops_trade_and_advances_historical_book(self):
        stop = Event()
        tape = Tape(external=True)
        tape.sim.set_emergency_event(stop)
        tape.start()
        tape.enter("WORKING")
        tape.through(2)
        capacity = Simulation._capacity

        def interrupt_before_fill(simulation, side):
            stop.set()
            return capacity(simulation, side)

        with patch.object(Simulation, "_capacity", interrupt_before_fill):
            tape.event("E", 3, 7, 101, "B", 1, 11)
        state = tape.sim.snapshot()
        self.assertEqual((tape.sim.stop_reason, state["fills"], state["position"]), ("manual", 0, 0))
        self.assertEqual(state["reference_bids"][0]["quantity"], 9)
        self.assertEqual([(r.kind, r.token) for r in tape.replies], [("A", "WORKING"), ("C", "WORKING")])

    def test_stop_during_strategy_callback_discards_returned_intent(self):
        stop = Event()

        class StopDuringDecision:
            def on_event(self, event, view, updates):
                if view.best_bid is None or view.best_ask is None:
                    return ()
                stop.set()
                return (Enter("B", view.best_bid, 2),)

        tape = Tape(strategy=StopDuringDecision())
        tape.sim.set_emergency_event(stop)
        tape.start()
        self.assertEqual(tape.sim.stop_reason, "manual")
        self.assertEqual(tape.sim.snapshot()["order_log"], [])
        self.assertIsNone(tape.sim.next_due())


if __name__ == "__main__":
    unittest.main()
