import json
import struct
import unittest

from dashboard.ouch import Command, Reply, decode_command, decode_reply, encode_command, encode_reply
from dashboard.simulation import Portfolio, Simulation
from dashboard.strategy_api import Enter


EPOCH = 1_779_433_600_000_000_000
BOOK = 71_536
NS_PER_US = 1000


class OuchWireTests(unittest.TestCase):
    def test_enter_network_offsets_and_soup_length(self):
        cmd = Command("O", "SIM00000000001", BOOK, "B", 103200, 45)
        frame = encode_command(cmd)
        self.assertEqual(len(frame), 117)
        self.assertEqual(frame[:4], bytes.fromhex("0073554f"))
        self.assertEqual(frame[4:18], cmd.token.encode())
        self.assertEqual(struct.unpack_from("!IcQiBB", frame, 18), (BOOK, b"B", 45, 103200, 0, 0))
        self.assertEqual(frame[68:100], b"ASELS-SIM".ljust(32))
        self.assertEqual(decode_command(frame), cmd)

    def test_replace_and_cancel_use_original_token(self):
        replaced = Command("U", "N", previous_token="O", price=101300, quantity=20)
        frame = encode_command(replaced)
        self.assertEqual(len(frame), 125)
        self.assertEqual(frame[:4], b"\x00\x7bUU")
        self.assertEqual(frame[4:32], b"O".ljust(14) + b"N".ljust(14))
        self.assertEqual(struct.unpack_from("!Qi", frame, 32), (20, 101300))
        self.assertEqual(decode_command(frame), replaced)
        canceled = Command("X", "O")
        self.assertEqual(encode_command(canceled), b"\x00\x10UX" + b"O".ljust(14))
        self.assertEqual(decode_command(encode_command(canceled)), canceled)

    def test_outbound_layouts_and_reject_signed_reason(self):
        replies = [
            (Reply("A", EPOCH, "ACK", BOOK, "S", 13, 120500, 42), 137),
            (Reply("U", EPOCH, "NEW", BOOK, "S", 13, 120500, 42, "ACK"), 145),
            (Reply("J", EPOCH, "ACK", reason=-14), 27),
            (Reply("C", EPOCH, "ACK", BOOK, "S", 13, reason=1), 37),
            (Reply("E", EPOCH, "ACK", BOOK, price=120500, quantity=19, match_id=(1 << 80) + 9), 68),
        ]
        for reply, size in replies:
            with self.subTest(reply=reply.kind):
                frame = encode_reply(reply)
                self.assertEqual(len(frame), size + 3)
                self.assertEqual(frame[:3], struct.pack("!Hc", size + 1, b"S"))
                self.assertEqual(struct.unpack_from("!Q", frame, 4)[0], EPOCH)
                self.assertEqual(decode_reply(frame), reply)
        with self.assertRaises(ValueError):
            decode_reply(encode_reply(replies[0][0])[:-1])
        with self.assertRaises(ValueError):
            decode_command(encode_reply(replies[0][0]))


class ReplayFixture:
    def __init__(self, *, strategy=None, **config):
        self.sim = Simulation({"quote_interval_ms": 50, **config}, strategy=strategy)
        self.sequence = 0

    def add(self, kind, us, *args):
        self.sequence += 1
        self.sim.apply([kind, self.sequence, EPOCH + us * NS_PER_US, *args])

    def order(self, us, order_id, side, price, quantity, rank_us=None):
        if rank_us is None:
            rank_us = us
        self.add("A", us, order_id, side, price, quantity, EPOCH + rank_us * NS_PER_US, 1)

    def start(self):
        self.add("S", 0, "O")
        self.add("R", 0, BOOK, "ASELS.E", 3, 1)
        self.add("L", 0, 0, 0, 100)
        self.add("O", 0, "P_SUREKLI_ISLEM")
        self.order(1, 101, "B", 100000, 10)
        self.order(2, 101, "S", 101000, 10)


class SimulationTests(unittest.TestCase):
    def test_order_priority_partials_and_pnl(self):
        fixture = ReplayFixture(latency_us=20, quote_size=5, max_position=8)
        fixture.start()
        self.assertEqual(fixture.sim.snapshot()["ouch_sent"], 2)
        # At the exact arrival timestamp the recorded trades precede both quotes.
        fixture.add("E", 22, 101, "B", 4)
        self.assertIsNone(fixture.sim.snapshot()["quote_bid"])
        fixture.order(23, 102, "B", 100000, 10)
        fixture.order(24, 102, "S", 101000, 20)
        state = fixture.sim.snapshot()
        self.assertEqual((state["quote_bid"], state["quote_ask"]), (100.0, 101.0))
        self.assertEqual((state["bid_ahead"], state["ask_ahead"]), (6, 10))
        quote_log = state["order_log"]
        self.assertEqual([line["type"] for line in quote_log], ["O", "O", "A", "A"])
        self.assertEqual([line["ouch_sequence"] for line in quote_log], [None, None, 1, 2])
        self.assertEqual([line["request_sequence"] for line in quote_log], [1, 2, 1, 2])
        self.assertEqual(int(quote_log[-1]["time_ns"]), EPOCH + 22 * NS_PER_US)
        fixture.add("E", 25, 102, "B", 2)  # Still ahead on the same price.
        fixture.add("D", 26, 101, "B")  # Cancellation advances the queue without filling.
        self.assertEqual(fixture.sim.snapshot()["fills"], 0)
        fixture.add("C", 27, 102, "B", 1)  # Legacy C lacks the price evidence to credit a simulated fill.
        fixture.add("E", 28, 102, "B", 4)
        bought = fixture.sim.snapshot()
        self.assertEqual((bought["position"], bought["average_price"], bought["quote_bid_quantity"]), (4, 100.0, 1))
        self.assertAlmostEqual(bought["unrealized_pnl"], 2.0)
        fixture.add("D", 29, 101, "S")
        fixture.add("E", 30, 102, "S", 4)
        sold = fixture.sim.snapshot()
        self.assertEqual((sold["position"], sold["average_price"], sold["fills"]), (0, None, 2))
        self.assertEqual((sold["cash"], sold["realized_pnl"], sold["total_pnl"]), (4.0, 4.0, 4.0))
        json.dumps(sold, allow_nan=False)

    def test_replace_price_loses_queue_and_disable_withdraws_while_paused(self):
        fixture = ReplayFixture(quote_interval_ms=1, latency_us=0)
        fixture.start()
        fixture.order(3, 102, "B", 100000, 400)
        fixture.order(1_005, 201, "B", 100100, 150)
        fixture.order(1_006, 202, "B", 100100, 40)
        state = fixture.sim.snapshot()
        self.assertEqual((state["quote_bid"], state["bid_ahead"]), (100.1, 150))
        sent, received = state["order_log"][-2:]
        self.assertEqual((sent["type"], received["type"], sent["token"]), ("U", "U", received["token"]))
        self.assertEqual(sent["previous_token"], received["previous_token"])
        fixture.sim.set_enabled(False)
        paused = fixture.sim.snapshot()
        self.assertFalse(paused["strategy_enabled"])
        self.assertIsNone(paused["quote_bid"])
        self.assertEqual([event["type"] for event in paused["order_log"][-2:]], ["C", "C"])

    def test_cumulative_replace_quantity_and_full_fill_do_not_overfill(self):
        fixture = ReplayFixture(quote_interval_ms=1, latency_us=0, quote_size=5, max_position=5)
        fixture.start()
        fixture.order(3, 102, "B", 100000, 40)
        fixture.add("D", 4, 101, "B")
        fixture.add("E", 5, 102, "B", 2)
        fixture.order(1_005, 201, "B", 100100, 300)
        fixture.order(1_006, 202, "B", 100100, 60)
        state = fixture.sim.snapshot()
        replace = state["order_log"][-2]
        self.assertEqual((replace["type"], replace["quantity"], state["quote_bid_quantity"]), ("U", 5, 3))
        fixture.add("D", 1_007, 201, "B")
        fixture.add("E", 1_008, 202, "B", 10)
        state = fixture.sim.snapshot()
        self.assertEqual(state["position"], 5)
        self.assertIsNone(state["quote_bid"])

    def test_phase_and_generation_reject_delayed_orders(self):
        fixture = ReplayFixture(latency_us=20)
        fixture.start()
        fixture.add("O", 5, "P_ESLESTIRME")
        fixture.add("O", 8, "P_SUREKLI_ISLEM")
        fixture.add("Y", 23)
        state = fixture.sim.snapshot()
        self.assertIsNone(state["quote_bid"])
        self.assertEqual([event["type"] for event in state["order_log"] if event["direction"] == "receive"], ["J", "J"])

    def test_ase_ls_filter_and_regressing_input_sequence(self):
        sim = Simulation()
        with self.assertRaises(ValueError):
            sim.apply(["R", 1, EPOCH, 1, "GARAN.E", 3, 1])
        with self.assertRaises(ValueError):
            sim.apply(["R", 1, EPOCH, BOOK, "ASELS.E", 3, 1])

    def test_signed_inventory_cost_and_commission(self):
        portfolio = Portfolio()
        portfolio.fill("S", 100, 6, 1)
        portfolio.fill("B", 90, 8, 2)
        self.assertEqual(portfolio.position, 2)
        self.assertEqual(portfolio.average, 90)
        self.assertEqual(portfolio.realized, 57)
        self.assertEqual(portfolio.cash, -123)


class ScheduledQuotes:
    def __init__(self, orders_by_sequence):
        self.orders_by_sequence = orders_by_sequence

    def on_event(self, event, view, updates):
        return self.orders_by_sequence.get(event.sequence, ())


class ShadowMatchingTests(unittest.TestCase):
    def start(self, *orders):
        strategy = ScheduledQuotes({6: orders})
        fixture = ReplayFixture(strategy=strategy, latency_us=0)
        fixture.start()
        return fixture

    def assert_budget(self, state, observed, strategy, historical, unallocated=0):
        audit = state["matching_audit"]
        self.assertEqual((audit["observed_quantity"], audit["strategy_quantity"],
                          audit["historical_quantity"], audit["unallocated_quantity"]),
                         (observed, strategy, historical, unallocated))
        self.assertEqual(audit["observed_quantity"], audit["strategy_quantity"] +
                         audit["historical_quantity"] + audit["unallocated_quantity"])
        for entry in audit["journal"]:
            if entry["event"] == "allocation":
                self.assertEqual(entry["observed_quantity"], entry["strategy_quantity"] +
                                 entry["historical_quantity"] + entry["unallocated_quantity"])

    def assert_level(self, state, book, price, quantity, own_quantity=0):
        rows = [row for row in state[book] if row["price"] == price]
        if quantity == 0:
            self.assertEqual(rows, [])
        else:
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["quantity"], rows[0].get("own_quantity", 0)), (quantity, own_quantity))

    def test_displaced_historical_order_remains_available_and_cancel_uses_its_own_id(self):
        fixture = self.start(Enter("B", 100000, 10))
        fixture.order(3, 999, "B", 99000, 40)
        fixture.order(4, 102, "B", 100000, 10)
        fixture.add("D", 5, 101, "B")
        fixture.add("E", 6, 102, "B", 10, 1001)
        filled = fixture.sim.snapshot()
        self.assertEqual((filled["position"], filled["average_price"], filled["cash"]), (10, 100.0, -1000.0))
        self.assertIsNone(filled["quote_bid"])
        self.assert_level(filled, "reference_bids", 100.0, 0)
        self.assert_level(filled, "bids", 100.0, 10)
        trade = filled["trades"][0]
        self.assertEqual((trade["source_kind"], trade["source_sequence"], trade["source_match_id"]),
                         ("E", fixture.sequence, "1001"))
        self.assert_budget(filled, 10, 10, 0)

        fixture.order(7, 103, "B", 100000, 5)
        fixture.add("E", 8, 103, "B", 4, 1002)
        reused = fixture.sim.snapshot()
        self.assert_level(reused, "reference_bids", 100.0, 1)
        self.assert_level(reused, "bids", 100.0, 11)
        self.assertEqual(reused["fills"], 1)
        self.assert_budget(reused, 14, 10, 4)

        fixture.add("D", 9, 103, "B")
        canceled = fixture.sim.snapshot()
        self.assert_level(canceled, "reference_bids", 100.0, 0)
        self.assert_level(canceled, "bids", 100.0, 6)
        self.assert_level(canceled, "asks", 101.0, 10)
        self.assert_budget(canceled, 14, 10, 4)

        fixture.add("O", 10, "P_ESLESTIRME")
        auction = fixture.sim.snapshot()
        self.assert_level(auction, "bids", 100.0, 0)
        self.assertEqual(auction["position"], 10)
        self.assertEqual(auction["matching_audit"]["reset_events"], 1)
        self.assert_budget(auction, 14, 10, 4)

    def test_single_budget_consumes_older_shadow_orders_before_partial_strategy_fill(self):
        strategy = ScheduledQuotes({6: (Enter("B", 100000, 10),), 10: (Enter("B", 100000, 10),)})
        fixture = ReplayFixture(strategy=strategy, latency_us=0)
        fixture.start()
        fixture.order(3, 999, "B", 99000, 40)
        fixture.order(4, 102, "B", 100000, 10)
        fixture.add("D", 5, 101, "B")
        fixture.add("E", 6, 102, "B", 10, 2001)
        fixture.order(7, 103, "B", 100000, 14)
        self.assertEqual(fixture.sim.snapshot()["bid_ahead"], 10)

        fixture.add("E", 8, 103, "B", 14, 2002)
        state = fixture.sim.snapshot()
        self.assertEqual((state["position"], state["quote_bid_quantity"], state["bid_ahead"]), (14, 6, 0))
        self.assertEqual([(trade["side"], trade["price"], trade["quantity"]) for trade in state["trades"]],
                         [("B", 100.0, 10), ("B", 100.0, 4)])
        self.assert_level(state, "reference_bids", 100.0, 0)
        self.assert_level(state, "bids", 100.0, 20, own_quantity=6)
        self.assert_budget(state, 24, 14, 10)

    def test_better_price_wins_only_on_the_resting_side_and_mark_stays_canonical(self):
        for side, quote_price, shadow_mid, depth, position in (
                ("B", 100200, 100.6, "bids", 6), ("S", 100800, 100.4, "asks", -6)):
            with self.subTest(side=side):
                opposite = "S" if side == "B" else "B"
                # Expose only the selected inside quote before observing its side of the market.
                fixture = self.start(Enter(side, quote_price, 6),
                                     Enter(opposite, 101000 if opposite == "S" else 100000, 6))
                fixture.order(3, 999, "B", 99000, 40)
                before = fixture.sim.snapshot()
                self.assertEqual((before["mid_price"], before["mark_price"]), (shadow_mid, 100.5))
                self.assert_level(before, depth, quote_price / 1000, 6, own_quantity=6)
                fixture.add("E", 4, 101, side, 6)  # Legacy unpriced E still carries visible-side volume.
                state = fixture.sim.snapshot()
                self.assertEqual((state["position"], state["fills"]), (position, 1))
                self.assertEqual([(trade["side"], trade["price"], trade["quantity"]) for trade in state["trades"]],
                                 [(side, quote_price / 1000, 6)])
                self.assert_level(state, depth, 100.0 if side == "B" else 101.0, 10)
                self.assert_level(state, "reference_" + depth, 100.0 if side == "B" else 101.0, 4)
                self.assert_budget(state, 6, 6, 0)

    def test_only_eligible_priced_executions_allocate_volume(self):
        fixture = self.start(Enter("B", 100200, 6), Enter("S", 100800, 6))
        cases = ((), (3001, 100000, "Y", "Y"), (3002, 100000, "N", "N"),
                 (3003, 100100, "N", "Y"), (3004, 100000, None, None), (0, 100000, "N", "Y"))
        for time, metadata in enumerate(cases, 3):
            with self.subTest(metadata=metadata):
                fixture.add("C", time, 101, "B", 1, *metadata)
                state = fixture.sim.snapshot()
                self.assertEqual((state["fills"], state["quote_bid_quantity"], state["quote_ask_quantity"]), (0, 6, 6))
                self.assert_level(state, "reference_bids", 100.0, 12 - time)
                self.assert_level(state, "bids", 100.0, 12 - time)
                self.assert_budget(state, 0, 0, 0)
        unsafe = fixture.sim.snapshot()["matching_audit"]
        self.assertEqual((unsafe["skipped_events"], unsafe["skipped_quantity"]), (len(cases), len(cases)))

        fixture.add("P", 9, BOOK, 100500, 7, 3005)  # An unkeyed trade print at the midpoint has no resting side.
        state = fixture.sim.snapshot()
        self.assertEqual((state["fills"], state["quote_bid_quantity"], state["quote_ask_quantity"]), (0, 6, 6))
        self.assert_budget(state, 0, 0, 0)

        fixture.add("C", 10, 101, "B", 2, 3006, 99900, "N", "Y")
        priced = fixture.sim.snapshot()
        self.assertEqual((priced["position"], priced["average_price"], priced["quote_bid_quantity"],
                          priced["quote_ask_quantity"]), (2, 100.2, 4, 6))
        self.assert_budget(priced, 2, 2, 0)

    def test_repeat_match_id_cannot_allocate_a_second_budget_even_on_opposite_side(self):
        fixture = self.start(Enter("B", 100200, 6), Enter("S", 100800, 6))
        fixture.add("E", 3, 101, "B", 2, 4001)
        fixture.add("E", 4, 101, "B", 2, 4001)
        fixture.add("E", 5, 101, "S", 2, 4001)
        repeated = fixture.sim.snapshot()
        self.assertEqual((repeated["position"], repeated["fills"], repeated["quote_ask_quantity"]), (2, 1, 6))
        self.assert_level(repeated, "reference_bids", 100.0, 6)
        self.assert_level(repeated, "reference_asks", 101.0, 8)
        self.assert_budget(repeated, 2, 2, 0)

        fixture.add("E", 6, 101, "S", 2, 4002)
        distinct = fixture.sim.snapshot()
        self.assertEqual((distinct["position"], distinct["fills"], distinct["realized_pnl"]), (0, 2, 1.2))
        self.assert_budget(distinct, 4, 4, 0)

    def test_crossing_shadow_quote_is_withdrawn_without_creating_a_fill(self):
        fixture = self.start(Enter("B", 100800, 6))
        fixture.order(3, 102, "S", 100400, 10)
        crossed = fixture.sim.snapshot()
        self.assertEqual((crossed["fills"], crossed["position"], crossed["quote_bid"]), (0, 0, None))
        self.assertEqual([line["type"] for line in crossed["order_log"]], ["O", "A", "C"])
        self.assertEqual((crossed["reference_best_bid"], crossed["reference_best_ask"]), (100.0, 100.4))
        self.assertFalse(crossed["matching_audit"]["suspended"])
        self.assertEqual(crossed["matching_audit"]["crossing_events"], 1)
        self.assertEqual(crossed["matching_audit"]["reconcile_events"], 0)
        self.assertEqual(crossed["matching_audit"]["journal"][-1]["event"], "resumed")
        self.assert_budget(crossed, 0, 0, 0)

        fixture.order(4, 999, "B", 99000, 20)
        recovered = fixture.sim.snapshot()
        self.assertFalse(recovered["matching_audit"]["suspended"])
        self.assertTrue(recovered["strategy_active"])

    def test_crossed_shadow_depth_reconciles_only_overlap_and_replay_resumes(self):
        strategy = ScheduledQuotes({6: (Enter("B", 100000, 10), Enter("S", 101000, 4)),
                                    16: (Enter("B", 99100, 3),)})
        fixture = ReplayFixture(strategy=strategy, latency_us=0)
        fixture.start()
        fixture.order(3, 999, "B", 99000, 40)
        fixture.order(4, 102, "B", 100000, 10)
        fixture.order(5, 999, "S", 102000, 30)
        fixture.order(6, 102, "S", 101000, 4)
        fixture.add("D", 7, 101, "B")
        fixture.add("D", 8, 101, "S")
        fixture.add("E", 9, 102, "B", 10, 7001)
        fixture.add("E", 10, 102, "S", 4, 7002)
        before = fixture.sim.snapshot()
        self.assertEqual((before["best_bid"], before["best_ask"]), (100.0, 101.0))
        self.assertEqual((before["reference_best_bid"], before["reference_best_ask"]), (99.0, 102.0))

        fixture.order(11, 103, "S", 99500, 20)
        reconciled = fixture.sim.snapshot()
        self.assertEqual((reconciled["best_bid"], reconciled["best_ask"]), (99.0, 99.5))
        self.assert_level(reconciled, "bids", 100.0, 0)
        self.assert_level(reconciled, "bids", 99.0, 40)
        self.assert_level(reconciled, "asks", 101.0, 4)
        self.assert_level(reconciled, "reference_asks", 101.0, 0)
        self.assertEqual((reconciled["position"], reconciled["fills"]), (6, 2))
        audit = reconciled["matching_audit"]
        self.assertFalse(audit["suspended"])
        self.assertEqual((audit["crossing_events"], audit["reconcile_events"], audit["reconciled_quantity"]), (1, 1, 10))
        repair = next(entry for entry in audit["journal"] if entry["event"] == "reconciliation")
        self.assertEqual((repair["buy_quantity"], repair["sell_quantity"]), (10, 0))
        self.assert_budget(reconciled, 14, 14, 0)

        fixture.order(12, 998, "B", 98000, 20)
        fixture.add("E", 13, 999, "B", 3, 7003)
        resumed = fixture.sim.snapshot()
        self.assertEqual((resumed["position"], resumed["fills"], resumed["trades"][-1]["price"]), (9, 3, 99.1))
        self.assert_budget(resumed, 17, 17, 0)

    def test_disabled_strategy_replays_exact_canonical_depth(self):
        fixture = ReplayFixture(enabled=False)
        fixture.start()
        fixture.order(3, 102, "B", 100000, 7)
        events = (("E", 4, 101, "B", 3), ("C", 5, 101, "S", 3, 5001, 101000, "N", "Y"),
                  ("C", 6, 101, "B", 1), ("D", 7, 102, "B"), ("Y", 8))
        for event in events:
            with self.subTest(event=event):
                fixture.add(*event)
                state = fixture.sim.snapshot()
                for depth in ("bids", "asks"):
                    shadow = [(row["price"], row["quantity"], row["orders"]) for row in state[depth]]
                    reference = [(row["price"], row["quantity"], row["orders"]) for row in state["reference_" + depth]]
                    self.assertEqual(shadow, reference)
                self.assertEqual((state["fills"], state["ouch_sent"], state["position"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
