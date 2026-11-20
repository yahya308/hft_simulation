import unittest
from unittest.mock import patch

from dashboard.multi_simulation import MultiSimulation
from dashboard.simulation import Simulation
from dashboard.strategy_api import Enter


EPOCH = 1_779_433_600_000_000_000
ASELS, THYAO = "ASELS.E", "THYAO.E"
ASELS_PORT, THYAO_PORT = 21001, 21002


class BuyOnce:
    def __init__(self):
        self.events = []
        self.sent = False

    def on_event(self, event, view, updates):
        self.events.append((event, view.symbol))
        if self.sent or view.best_bid is None or view.best_ask is None:
            return ()
        self.sent = True
        return (Enter("B", view.best_bid + 100, min(2, view.buy_capacity)),)


class Tape:
    def __init__(self, symbols, *, latency_us=10):
        self.symbols = symbols
        self.strategies = {}
        self.raw_sequences = {}
        configs = {ASELS: {"latency_us": latency_us, "max_position": 1},
                   THYAO: {"latency_us": latency_us, "max_position": 3}}
        self.sim = MultiSimulation(configs, sources={symbol: route[0] for symbol, route in symbols.items()},
                                   strategy_factory=self.make_strategy)

    def make_strategy(self, symbol):
        self.strategies[symbol] = BuyOnce()
        return self.strategies[symbol]

    def event(self, port, kind, us, book_id, *args):
        source_sequence = self.raw_sequences.get(port, 1000) + 1
        self.raw_sequences[port] = source_sequence
        self.sim.apply([kind, self.sim.sequence + 1, EPOCH + us * 1000, book_id, *args],
                       source=port, source_sequence=source_sequence)

    def order(self, symbol, us, order_id, side, price):
        port, book_id, _ = self.symbols[symbol]
        self.event(port, "A", us, book_id, order_id, side, price, 10, EPOCH + us * 1000, 1)

    def register(self, symbol):
        port, book_id, bid = self.symbols[symbol]
        if port not in self.raw_sequences:
            self.event(port, "S", 0, None, "O")
        self.event(port, "R", 0, book_id, symbol, 3, 1)
        self.event(port, "L", 0, book_id, 0, 0, 100)
        self.event(port, "O", 0, book_id, "P_SUREKLI_ISLEM")
        self.order(symbol, 1, 101, "B", bid)
        self.order(symbol, 2, 201, "S", bid + 1000)


class MultiSimulationTests(unittest.TestCase):
    def test_partition_scoped_keys_and_arrival_order_across_books(self):
        tape = Tape({ASELS: (ASELS_PORT, 7, 100000), THYAO: (THYAO_PORT, 7, 300000)})
        tape.register(THYAO)
        tape.register(ASELS)
        self.assertIsNot(tape.strategies[ASELS], tape.strategies[THYAO])
        calls_before = len(tape.strategies[ASELS].events)
        tape.order(THYAO, 12, 102, "B", 299000)
        self.assertIsNone(tape.sim.snapshot(ASELS)["quote_bid"])
        self.assertIsNone(tape.sim.snapshot(THYAO)["quote_bid"])

        delivered = []
        receive_next = Simulation.receive_next

        def receive(sim):
            delivered.append(sim.symbol)
            receive_next(sim)

        with patch.object(Simulation, "receive_next", receive):
            tape.order(THYAO, 13, 202, "S", 302000)
        self.assertEqual(delivered, [THYAO, ASELS])
        self.assertEqual(len(tape.strategies[ASELS].events), calls_before)
        for symbol, expected_price, expected_size in ((ASELS, 100.1, 1), (THYAO, 300.1, 2)):
            state = tape.sim.snapshot(symbol)
            self.assertEqual((state["book_id"], state["quote_bid"], state["quote_bid_quantity"]),
                             (7, expected_price, expected_size))
            self.assertEqual(state["order_log"][-1]["time_ns"], str(EPOCH + 12_000))

        tape.event(ASELS_PORT, "E", 14, 7, 101, "B", 1, 55)
        tape.event(THYAO_PORT, "E", 15, 7, 101, "B", 2, 55)
        for state, symbol, port, quantity in ((tape.sim.snapshot(ASELS), ASELS, ASELS_PORT, 1),
                                               (tape.sim.snapshot(THYAO), THYAO, THYAO_PORT, 2)):
            self.assertEqual((state["symbol"], state["position"], state["fills"]), (symbol, quantity, 1))
            self.assertEqual(state["trades"][-1]["source_port"], port)
            self.assertEqual(state["trades"][-1]["source_mold_sequence"], tape.raw_sequences[port])
            event, strategy_symbol = tape.strategies[symbol].events[-1]
            self.assertEqual((event.source_port, event.source_sequence, strategy_symbol),
                             (port, tape.raw_sequences[port], symbol))
        self.assertEqual([item["position"] for item in tape.sim.summaries()], [1, 2])

    def test_partition_close_cancels_only_its_own_pending_arrivals(self):
        tape = Tape({ASELS: (ASELS_PORT, 7, 100000), THYAO: (THYAO_PORT, 7, 300000)})
        tape.register(ASELS)
        tape.register(THYAO)
        tape.event(ASELS_PORT, "S", 3, None, "C")
        self.assertEqual([entry["type"] for entry in tape.sim.snapshot(ASELS)["order_log"]], ["O", "J"])
        self.assertEqual([entry["type"] for entry in tape.sim.snapshot(THYAO)["order_log"]], ["O"])
        tape.order(THYAO, 13, 102, "B", 299000)
        self.assertEqual([entry["type"] for entry in tape.sim.snapshot(THYAO)["order_log"]], ["O", "A"])
        tape.sim.finish()
        self.assertEqual([entry["type"] for entry in tape.sim.snapshot(THYAO)["order_log"]], ["O", "A", "C"])

    def test_two_books_on_one_partition_and_independent_positions(self):
        tape = Tape({ASELS: (ASELS_PORT, 7, 100000), THYAO: (ASELS_PORT, 8, 300000)}, latency_us=0)
        tape.register(ASELS)
        tape.register(THYAO)
        tape.event(ASELS_PORT, "E", 3, 8, 101, "B", 2, 9)
        self.assertEqual((tape.sim.snapshot(ASELS)["position"], tape.sim.snapshot(THYAO)["position"]), (0, 2))
        tape.event(ASELS_PORT, "S", 4, None, "C")
        self.assertFalse(tape.sim.snapshot(ASELS)["strategy_active"])
        self.assertFalse(tape.sim.snapshot(THYAO)["strategy_active"])

    def test_legacy_asels_directory_alias_and_selected_equity_validation(self):
        legacy = Simulation()
        legacy.apply(["R", 1, EPOCH, 7, "ASELS", 3, 1])
        self.assertEqual(legacy.snapshot()["symbol"], "ASELS")
        selected = Simulation(symbol=THYAO)
        with self.assertRaises(ValueError):
            selected.apply(["R", 1, EPOCH, 7, ASELS, 3, 1])
        selected.apply(["R", 2, EPOCH, 7, THYAO, 3, 1])
        self.assertEqual(selected.snapshot()["symbol"], THYAO)


if __name__ == "__main__":
    unittest.main()
