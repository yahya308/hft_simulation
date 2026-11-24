import json
import socket
import tempfile
import unittest
from pathlib import Path
from queue import Queue

from dashboard.multi_simulation import MultiSimulation
from dashboard.ouch import Command, decode_login_accepted, decode_reply, encode_command
from dashboard.ouch_gateway import OuchGateway
from dashboard.run_audit import RunAudit, verify_run
from dashboard.strategy_api import Enter
from test_ouch_gateway import log_in, read_frame


EPOCH = 1_779_433_600_000_000_000
ASELS, THYAO = "ASELS.E", "THYAO.E"
ROUTES = {ASELS: (7, 100_000), THYAO: (8, 300_000)}


def ns(us):
    return EPOCH + us * 1_000


class Tape:
    def __init__(self, *, symbols=(ASELS,), strategy=None, external=False, audit=None, **settings):
        self.symbols = symbols
        factory = None if strategy is None else lambda symbol: strategy
        self.sim = MultiSimulation({symbol: {"latency_us": 0, **settings} for symbol in symbols},
                                   strategy_factory=factory)
        self.replies = []
        if external:
            self.sim.set_reply_sink(lambda frame: self.replies.append(decode_reply(frame)))
        if audit is not None:
            self.sim.set_audit_sink(audit)

    def event(self, kind, us, book, *fields):
        self.sim.apply([kind, self.sim.sequence + 1, ns(us), book, *fields])

    def order(self, us, book, order, side, price, quantity=10):
        self.event("A", us, book, order, side, price, quantity, ns(us), 1)

    def start(self):
        self.event("S", 0, None, "O")
        for symbol in self.symbols:
            book, _ = ROUTES[symbol]
            self.event("R", 0, book, symbol, 3, 1)
            self.event("L", 0, book, 0, 0, 100)
            self.event("O", 0, book, "P_SUREKLI_ISLEM")
        for us, order, side, increment in ((1, 101, "B", 0), (2, 201, "S", 1_000)):
            for symbol in self.symbols:
                book, price = ROUTES[symbol]
                self.order(us, book, order, side, price + increment)

    def enter(self, token, symbol=ASELS, side="B", quantity=2):
        book, price = ROUTES[symbol]
        self.sim.external_order(Command("O", token, book, side, price + (100 if side == "B" else 900), quantity))

    def through(self, us):
        self.sim.advance_through(ns(us))


class BuyOnTwoSidedBook:
    def __init__(self):
        self.observations = []
        self.sent = False

    def on_event(self, event, view, updates):
        self.observations.append((event, view, updates))
        if self.sent or view.best_bid is None or view.best_ask is None:
            return ()
        self.sent = True
        return (Enter("B", view.best_bid + 100, 2),)


class EnterpriseRuntimeTests(unittest.TestCase):
    def test_market_order_and_reply_have_separate_virtual_arrivals(self):
        strategy = BuyOnTwoSidedBook()
        audit = []
        tape = Tape(strategy=strategy, audit=lambda kind, data: audit.append((kind, data)),
                    market_latency_us=7, latency_us=11, reply_latency_us=13)
        tape.start()
        tape.through(8)
        self.assertFalse(strategy.sent)
        self.assertEqual(tape.sim.next_due(), ns(9))
        tape.through(9)
        self.assertTrue(strategy.sent)
        self.assertEqual(strategy.observations[-1][0].exchange_ns, ns(2))
        self.assertEqual(tape.sim.snapshot()["order_log"][-1]["time_ns"], str(ns(9)))
        self.assertEqual(tape.sim.next_due(), ns(20))
        tape.through(19)
        self.assertIsNone(tape.sim.snapshot()["quote_bid"])
        tape.through(20)
        self.assertEqual(tape.sim.snapshot()["quote_bid"], 100.1)
        self.assertEqual(tape.sim.next_due(), ns(33))

        tape.order(24, 7, 102, "B", 99_000)
        tape.order(27, 7, 103, "B", 98_000)
        tape.through(31)
        _, before, before_updates = strategy.observations[-1]
        self.assertEqual((before.orders, before.pending_sides, before_updates), ((), ("B",), ()))
        tape.through(34)
        _, after, updates = strategy.observations[-1]
        self.assertEqual((after.orders[0].remaining_quantity, after.pending_sides), (2, ()))
        self.assertEqual([(update.kind, update.exchange_ns) for update in updates], [("A", ns(20))])
        self.assertEqual([data["time_ns"] for kind, data in audit if kind == "reply_delivery"], [str(ns(33))])

    def test_seeded_jitter_repeats_and_default_is_fixed(self):
        def trace(seed=None, jitter=0):
            audit = []
            settings = {} if seed is None else {"latency_seed": seed, "market_jitter_us": jitter,
                                                  "order_jitter_us": jitter, "reply_jitter_us": jitter}
            tape = Tape(strategy=BuyOnTwoSidedBook(), audit=lambda kind, data: audit.append((kind, data)),
                        market_latency_us=50, latency_us=50, reply_latency_us=50, **settings)
            tape.start()
            tape.through(1_000)
            self.assertIsNone(tape.sim.next_due())
            state = tape.sim.snapshot()
            return audit, state["order_log"], state["quote_bid"]

        self.assertEqual(trace(), trace(seed=9))
        self.assertEqual(trace(seed=42, jitter=20), trace(seed=42, jitter=20))
        self.assertNotEqual(trace(seed=42, jitter=20), trace(seed=43, jitter=20))

    def test_order_size_limit_rejects_only_excess(self):
        tape = Tape(external=True, order_size_limit=2)
        tape.start()
        tape.enter("OVER", quantity=3)
        tape.through(2)
        tape.enter("LIMIT", quantity=2)
        tape.through(2)
        self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies],
                         [("J", "OVER", -3), ("A", "LIMIT", 0)])
        self.assertEqual(tape.sim.snapshot()["quote_bid_quantity"], 2)

    def test_global_rate_window_resets_and_does_not_block_cancel(self):
        tape = Tape(symbols=(ASELS, THYAO), external=True, order_rate_limit=1)
        tape.start()
        tape.enter("FIRST")
        tape.through(2)
        tape.enter("SAMEWINDOW", THYAO)
        tape.through(2)
        tape.sim.external_order(Command("X", "FIRST"))
        tape.through(2)
        tape.through(1_000_002)
        tape.enter("NEXTWINDOW", THYAO)
        tape.through(1_000_002)
        self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies],
                         [("A", "FIRST", 0), ("J", "SAMEWINDOW", -7),
                          ("C", "FIRST", 1), ("A", "NEXTWINDOW", 0)])

    def test_gross_limit_reserves_working_orders_across_books(self):
        tape = Tape(symbols=(ASELS, THYAO), external=True, gross_limit_tl=400)
        tape.start()
        tape.enter("FIRST")
        tape.through(2)
        tape.enter("NOHEADROOM", THYAO, quantity=1)
        tape.through(2)
        self.assertIsNone(tape.sim.snapshot(THYAO)["quote_bid"])
        tape.sim.external_order(Command("X", "FIRST"))
        tape.through(2)
        tape.enter("HEADROOM", THYAO, quantity=1)
        tape.through(2)
        self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies],
                         [("A", "FIRST", 0), ("J", "NOHEADROOM", -8),
                          ("C", "FIRST", 1), ("A", "HEADROOM", 0)])

    def test_manual_stop_cancels_working_and_rejects_pending_then_latches(self):
        audit = []
        tape = Tape(external=True, latency_us=5, reply_latency_us=7,
                    audit=lambda kind, data: audit.append((kind, data)))
        tape.start()
        tape.enter("WORKING")
        tape.through(7)
        self.assertEqual(tape.replies, [])
        self.assertIsNotNone(tape.sim.snapshot()["quote_bid"])
        tape.enter("PENDING", side="S")
        tape.sim.emergency_stop()
        tape.sim.emergency_stop("redundant")
        self.assertEqual(tape.sim.stop_reason, "manual")
        self.assertIsNone(tape.sim.snapshot()["quote_bid"])
        with self.assertRaises(ValueError):
            tape.sim.set_enabled(True)
        tape.through(14)
        self.assertEqual([(r.kind, r.token, r.reason) for r in tape.replies],
                         [("A", "WORKING", 0), ("C", "WORKING", 9), ("J", "PENDING", -9)])
        self.assertEqual([data["reason"] for kind, data in audit if kind == "risk_stop"], ["manual"])

    def test_mark_loss_latches_and_cancels_remaining_quantity(self):
        tape = Tape(external=True, loss_limit_tl=4)
        tape.start()
        tape.enter("FIRST")
        tape.through(2)
        tape.event("E", 3, 7, 101, "B", 1, 11)
        self.assertEqual(tape.sim.snapshot()["position"], 1)
        tape.event("D", 4, 7, 101, "B")
        tape.order(5, 7, 102, "B", 90_000)
        self.assertEqual(tape.sim.stop_reason, "loss_limit")
        state = tape.sim.snapshot()
        self.assertAlmostEqual(state["total_pnl"], -4.6)
        self.assertIsNone(state["quote_bid"])
        self.assertEqual([reply.kind for reply in tape.replies], ["A", "E", "C"])

    def test_run_audit_persists_order_and_detects_modified_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario = {"capture": {"name": "fixture.pcap"}, "settings": {"latency_seed": 42}}
            audit = RunAudit(Path(directory), scenario)
            tape = Tape(external=True, audit=audit.record, reply_latency_us=2)
            tape.start()
            tape.enter("AUDITED")
            tape.through(4)
            tape.sim.finish()
            results = {"fill_count": tape.sim.snapshot()["fills"], "reply_count": len(tape.replies)}
            audit.finish("complete", results)
            manifest = verify_run(audit.directory)
            self.assertEqual((manifest["scenario"], manifest["results"]), (scenario, results))
            ledger = audit.directory / "events.ndjson"
            entries = [json.loads(line) for line in ledger.read_bytes().splitlines()]
            self.assertEqual([entry["index"] for entry in entries], list(range(1, manifest["records"] + 1)))
            self.assertEqual([entry["data"]["type"] for entry in entries if entry["kind"] == "ouch"],
                             ["O", "A", "C"])
            self.assertEqual(len([entry for entry in entries if entry["kind"] == "reply_delivery"]), 2)
            with ledger.open("ab") as stream:
                stream.write(b"{}\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify_run(audit.directory)

    def test_loopback_command_reaches_engine_at_configured_delays(self):
        inbox = Queue()
        with OuchGateway(inbox.put) as gateway:
            try:
                peer = socket.create_connection(gateway.address, timeout=2)
            except PermissionError as error:
                self.skipTest(f"Loopback TCP is unavailable in this execution environment: {error}")
            with peer:
                self.assertEqual(decode_login_accepted(log_in(peer)), (gateway.session, 1))
                tape = Tape(external=True, latency_us=5, reply_latency_us=7)
                tape.sim.set_reply_sink(gateway.publish)
                tape.start()
                peer.sendall(encode_command(Command("O", "FROMCLIENT", 7, "B", 100_100, 2)))
                request = inbox.get(timeout=2)
                self.assertEqual((request.sequence, request.generation), (1, gateway.generation))
                tape.sim.external_order(request.command)
                tape.through(7)
                self.assertEqual(tape.sim.snapshot()["quote_bid_quantity"], 2)
                self.assertEqual(tape.sim.next_due(), ns(14))
                tape.through(14)
                reply = decode_reply(read_frame(peer))
                self.assertEqual((reply.kind, reply.token, reply.time_ns), ("A", "FROMCLIENT", ns(7)))


if __name__ == "__main__":
    unittest.main()
