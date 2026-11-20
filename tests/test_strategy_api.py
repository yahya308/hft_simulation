import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.ouch import decode_command
from dashboard.simulation import Simulation
from dashboard.strategy_api import Cancel, Enter, Replace, StrategyError


EPOCH = 1_779_433_600_000_000_000
BOOK = 71_536


class ScriptedStrategy:
    def __init__(self, decisions):
        self.decisions = decisions
        self.observations = {}

    def on_event(self, event, view, updates):
        self.observations[event.sequence] = event, view, updates
        decide = self.decisions.get(event.sequence)
        return decide(view) if decide else ()


class Replay:
    def __init__(self, strategy, *, latency_us=0):
        self.sim = Simulation({"latency_us": latency_us}, strategy=strategy)
        self.sequence = 0
        self.event("S", 0, "O")
        self.event("R", 0, BOOK, "ASELS.E", 3, 1)
        self.event("L", 0, 0, 0, 100)
        self.event("O", 0, "P_SUREKLI_ISLEM")
        self.order(1, 101, "B", 100000, 10)
        self.order(2, 201, "S", 101000, 10)

    def event(self, kind, us, *args):
        self.sequence += 1
        self.sim.apply([kind, self.sequence, EPOCH + us * 1000, *args])

    def order(self, us, number, side, price, quantity):
        self.event("A", us, number, side, price, quantity, EPOCH + us * 1000, 1)


class StrategyApiTests(unittest.TestCase):
    def test_market_view_and_equal_timestamp_updates(self):
        strategy = ScriptedStrategy({6: lambda view: (Enter("S", view.best_ask, 3),
                                                       Enter("B", view.best_bid, 2))})
        replay = Replay(strategy)
        replay.event("E", 2, 201, "S", 4)
        event, view, updates = strategy.observations[7]
        self.assertEqual((event.exchange_ns, view.exchange_ns, view.sequence), (EPOCH + 2000, EPOCH + 2000, 7))
        self.assertEqual((view.best_bid, view.best_ask, view.best_ask_quantity), (100000, 101000, 6))
        self.assertEqual(set(view.pending_sides), {"B", "S"})
        self.assertEqual((view.orders, updates), ((), ()))
        with self.assertRaises(FrozenInstanceError):
            view.position = 7
        with self.assertRaises(FrozenInstanceError):
            event.kind = "D"

        replay.order(3, 102, "B", 100000, 20)
        _, view, updates = strategy.observations[8]
        self.assertEqual(len(view.orders), 2)
        self.assertEqual([update.kind for update in updates], ["A", "A"])
        self.assertEqual(replay.sim.snapshot()["ask_ahead"], 6)
        self.assertEqual(replay.sim.snapshot()["bid_ahead"], 10)
        reply_log = [line for line in replay.sim.snapshot()["order_log"] if line["direction"] == "receive"]
        self.assertEqual([(line["side"], line["request_sequence"]) for line in reply_log], [("S", 1), ("B", 2)])
        replay.order(4, 103, "B", 100000, 10)
        self.assertEqual(strategy.observations[9][2], ())

    def test_replace_remaining_adjusts_for_fills_in_flight(self):
        strategy = ScriptedStrategy({6: lambda view: (Enter("B", view.best_bid, 8),),
                                     9: lambda view: (Replace(view.orders[0].order_id, view.best_bid, 4),)})
        replay = Replay(strategy, latency_us=10)
        replay.order(13, 102, "B", 100000, 30)
        replay.event("D", 14, 101, "B")
        replay.event("E", 15, 102, "B", 2)
        _, before, _ = strategy.observations[9]
        self.assertEqual((before.orders[0].remaining_quantity, before.orders[0].executed_quantity), (6, 2))
        sent = replay.sim.snapshot()["order_log"][-1]
        replacement = decode_command(bytes.fromhex(sent["frame_hex"]))
        self.assertEqual((replacement.kind, replacement.quantity), ("U", 6))
        replay.event("E", 20, 102, "B", 1)
        replay.order(26, 103, "B", 100000, 2)
        _, after, updates = strategy.observations[11]
        self.assertEqual([update.kind for update in updates], ["U"])
        self.assertEqual((after.orders[0].remaining_quantity, after.orders[0].executed_quantity), (3, 3))
        self.assertEqual(after.orders[0].order_id, before.orders[0].order_id)

    def test_failure_withdraws_orders_and_preserves_fill_ledger(self):
        def fail(_view):
            raise RuntimeError("custom strategy failed")

        strategy = ScriptedStrategy({6: lambda view: (Enter("B", view.best_bid, 5),), 9: fail})
        replay = Replay(strategy)
        replay.order(3, 102, "B", 100000, 10)
        replay.event("D", 4, 101, "B")
        with self.assertRaisesRegex(StrategyError, "custom strategy failed"):
            replay.event("E", 5, 102, "B", 2)
        final = replay.sim.snapshot()
        self.assertEqual((final["position"], final["cash"], final["fills"]), (2, -200.0, 1))
        self.assertIsNone(final["quote_bid"])
        self.assertEqual([line["type"] for line in final["order_log"]], ["O", "A", "E", "C"])

    def test_malformed_decisions_commit_no_prefix(self):
        cases = {"unknown_intent": lambda view: (Enter("B", view.best_bid, 1), object()),
                 "boolean_quantity": lambda view: (Enter("B", view.best_bid, 1), Enter("S", view.best_ask, True))}
        for name, invalid in cases.items():
            with self.subTest(case=name):
                strategy = ScriptedStrategy({7: invalid})
                replay = Replay(strategy)
                with self.assertRaises(StrategyError):
                    replay.order(3, 102, "B", 100000, 10)
                self.assertEqual(replay.sim.snapshot()["order_log"], [])

    def test_pending_side_rejects_second_request_without_sending_it(self):
        strategy = ScriptedStrategy({6: lambda view: (Enter("B", view.best_bid, 2), Enter("B", view.best_bid, 2))})
        replay = Replay(strategy)
        replay.order(3, 102, "B", 100000, 10)
        log = replay.sim.snapshot()["order_log"]
        self.assertEqual([line["type"] for line in log], ["O", "J", "A"])
        self.assertEqual([line["request_sequence"] for line in log], [1, 2, 1])
        self.assertEqual(len(strategy.observations[7][1].orders), 1)

    def test_crossing_intent_rejects_and_cancel_uses_stable_order_identity(self):
        strategy = ScriptedStrategy({6: lambda view: (Enter("B", view.best_ask, 2),),
                                     7: lambda view: (Enter("B", view.best_bid, 2),),
                                     8: lambda view: (Cancel(view.orders[0].order_id),)})
        replay = Replay(strategy)
        replay.order(3, 102, "B", 100000, 10)
        replay.order(4, 103, "B", 100000, 10)
        replay.order(5, 104, "B", 100000, 10)
        self.assertEqual([strategy.observations[seq][2][0].kind for seq in (7, 8, 9)], ["J", "A", "C"])
        self.assertIsNone(replay.sim.snapshot()["quote_bid"])


if __name__ == "__main__":
    unittest.main()
