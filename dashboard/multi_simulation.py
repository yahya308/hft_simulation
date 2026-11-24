"""Route ordered ITCH rows to independent equity simulations."""

from collections.abc import Callable, Mapping
from collections import deque
from fractions import Fraction
from heapq import heapify, heappop, heappush
from threading import Event

from .portfolio_risk import price_shock
from .ouch import Command
from .simulation import Simulation, canonical_symbol, _REJECT_BOOK, _REJECT_TOKEN
from .strategy_api import Strategy, StrategyError


class MultiSimulation:
    def __init__(self, configs: Mapping[str, dict], *,
                 strategy_factory: Callable[[str], Strategy | None] | None = None,
                 sources: Mapping[str, int] | None = None) -> None:
        if not configs:
            raise ValueError("Replay için en az bir pay seçilmeli.")
        self._simulations: dict[str, Simulation] = {}
        strategy_instances = set()
        try:
            for symbol, config in configs.items():
                symbol = canonical_symbol(symbol)
                if symbol in self._simulations:
                    raise ValueError(f"Pay birden fazla kez seçildi: {symbol}.")
                strategy = strategy_factory(symbol) if strategy_factory is not None else None
                if strategy is not None:
                    identity = id(strategy)
                    if identity in strategy_instances:
                        raise ValueError("Her pay için ayrı bir strateji örneği gerekli.")
                    strategy_instances.add(identity)
                self._simulations[symbol] = Simulation(config, symbol=symbol, strategy=strategy)
                self._simulations[symbol].risk_guard = self
        except BaseException:
            self.close_strategies()
            raise
        self.symbols = tuple(self._simulations)
        self.sequence = 0
        self.exchange_ns = 0
        self.scheduled_dispatches = 0
        self._sources = self._validate_sources(sources)
        self._symbols_by_source = self._group_sources()
        self._book_symbols: dict[tuple[int | None, int], str] = {}
        self._source_sequences: dict[int | None, int] = {}
        self._arrivals: list[tuple[int, int, int, int, str]] = []
        self._scheduled = {}
        self._rate_window = deque()
        self._pnl_by_symbol = {}
        self._pnl_total = Fraction(0)
        self._tokens = set()
        self._last_external_sequence = 0
        self._last_external_arrival = 0
        self._emergency_event: Event | None = None
        self._atomic_emergency = False
        self.audit_sink = None
        self.reply_sink = None
        self.stop_reason = None

    def _validate_sources(self, sources: Mapping[str, int] | None) -> dict[str, int]:
        if sources is None:
            return {}
        validated = {}
        for symbol, port in sources.items():
            symbol = canonical_symbol(symbol)
            if symbol in validated or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError(f"Pay için geçersiz ITCH kaynağı: {symbol}.")
            validated[symbol] = port
        if validated.keys() != self._simulations.keys():
            raise ValueError("Seçilen her pay için bir ITCH kaynağı gerekli.")
        return validated

    def _group_sources(self) -> dict[int | None, list[str]]:
        partitions: dict[int | None, list[str]] = {}
        for symbol in self.symbols:
            partitions.setdefault(self._sources.get(symbol), []).append(symbol)
        return partitions

    @property
    def phase(self) -> str:
        return self.phase_for(self.symbols[0])

    def phase_for(self, symbol: str) -> str:
        return self._simulations[canonical_symbol(symbol)].phase

    def _schedule(self, symbol: str, simulation: Simulation) -> None:
        arrival = simulation.next_action()
        if arrival == self._scheduled.get(symbol):
            return
        if arrival is None:
            self._scheduled.pop(symbol, None)
        else:
            self._scheduled[symbol] = arrival
            heappush(self._arrivals, (*arrival, symbol))

    def next_due(self) -> int | None:
        while self._arrivals:
            *arrival, symbol = self._arrivals[0]
            if self._scheduled.get(symbol) == tuple(arrival):
                return arrival[0]
            heappop(self._arrivals)
        return None

    def _advance_before(self, time_ns: int) -> None:
        # The feed, including every book at an identical timestamp, beats OUCH arrival ties.
        self._check_emergency()
        while (due := self.next_due()) is not None and due < time_ns:
            self._dispatch_next(due)

    def _dispatch_next(self, due: int) -> None:
        *_, symbol = heappop(self._arrivals)
        del self._scheduled[symbol]
        simulation = self._simulations[symbol]
        try:
            simulation.receive_next()
        except StrategyError as error:
            self.finish()
            raise StrategyError(error.sequence, f"{symbol}: {error.detail}") from error
        self.scheduled_dispatches += 1
        self._check_emergency(due)
        self._schedule(symbol, simulation)

    def advance_through(self, time_ns: int) -> None:
        self._advance_before(time_ns + 1)
        self.exchange_ns = max(self.exchange_ns, time_ns)
        self._check_emergency()

    def advance_recorded(self, time_ns: int, dispatches: int) -> None:
        """Reach a recorded intake boundary without draining extra equal-time work."""
        if time_ns < self.exchange_ns or dispatches < self.scheduled_dispatches:
            raise ValueError("Kayıtlı kontrol sınırı mevcut motor durumunun gerisinde.")
        while self.scheduled_dispatches < dispatches:
            due = self.next_due()
            if due is None or due > time_ns:
                raise ValueError("Kayıtlı kontrol sınırına bekleyen işlemlerle ulaşılamadı.")
            self._dispatch_next(due)
        self.exchange_ns = time_ns

    def _apply_market(self, symbol: str, kind: str, args: list,
                      source: int | None, source_sequence: int | None) -> None:
        simulation = self._simulations[symbol]
        try:
            simulation.apply([kind, self.sequence, self.exchange_ns, *args],
                             source_port=source, source_sequence=source_sequence)
        except StrategyError as error:
            self.finish()
            raise StrategyError(error.sequence, f"{symbol}: {error.detail}") from error
        self._schedule(symbol, simulation)

    def _clear_arrivals(self, symbols: list[str]) -> None:
        affected = set(symbols)
        self._arrivals[:] = [item for item in self._arrivals if item[-1] not in affected]
        heapify(self._arrivals)
        for symbol in affected:
            self._scheduled.pop(symbol, None)

    def apply(self, event: list, *, source: int | None = None, source_sequence: int | None = None) -> None:
        kind, sequence, time_ns, book_id, *args = event
        if sequence <= self.sequence:
            raise ValueError("ITCH sırası artmıyor.")
        if source not in self._symbols_by_source:
            raise ValueError(f"Replay akışında seçilmeyen bir ITCH kaynağı var: {source}.")
        if source_sequence is None and source is not None:
            source_sequence = sequence
        if source_sequence is not None:
            if source_sequence <= self._source_sequences.get(source, 0):
                raise ValueError(f"ITCH kaynağının sıra numarası artmıyor: {source}.")
            self._source_sequences[source] = source_sequence
        time_ns = max(time_ns, self.exchange_ns)
        self._advance_before(time_ns)
        self.sequence, self.exchange_ns = sequence, time_ns

        if kind == "S":
            if book_id is not None:
                raise ValueError("ITCH sistem olayında defter kimliği bulunamaz.")
            symbols = self._symbols_by_source[source]
            if args[0] == "C":
                self._clear_arrivals(symbols)
            for symbol in symbols:
                self._apply_market(symbol, kind, args, source, source_sequence)
            return

        if type(book_id) is not int or not 0 <= book_id < 2**32:
            raise ValueError(f"Geçersiz ITCH defter kimliği: {book_id}.")
        key = (source, book_id)
        if kind == "R":
            source_symbol, decimals, lot = args
            symbol = canonical_symbol(source_symbol)
            if self._book_symbols.get(key) not in (None, symbol):
                raise ValueError(f"Çakışan ITCH defter kimliği: {book_id}.")
            if symbol in self._simulations:
                if self._sources.get(symbol) != source:
                    raise ValueError(f"{symbol} beklenmeyen ITCH kaynağında bulundu: {source}.")
                self._apply_market(symbol, kind, [book_id, symbol, decimals, lot], source, source_sequence)
            self._book_symbols[key] = symbol
            return

        symbol = self._book_symbols.get(key)
        if symbol is None:
            raise ValueError(f"ITCH defter dizini bulunamadı: {book_id} (sıra {sequence}).")
        if symbol in self._simulations:
            self._apply_market(symbol, kind, args, source, source_sequence)

    def set_enabled(self, enabled: bool) -> None:
        self._check_emergency()
        if enabled and self.stop_reason is not None:
            raise ValueError("Risk durdurması kilitli. Yeni bir replay için başa al.")
        for symbol, simulation in self._simulations.items():
            simulation.sync_clock(self.exchange_ns)
            simulation.set_enabled(enabled)
            self._schedule(symbol, simulation)

    def set_audit_sink(self, sink) -> None:
        self.audit_sink = sink
        for simulation in self._simulations.values():
            simulation.audit_sink = sink

    def set_reply_sink(self, sink, *, context_sink=None) -> None:
        self.reply_sink = sink
        for simulation in self._simulations.values():
            simulation.reply_sink = sink
            simulation.reply_context_sink = context_sink

    def set_emergency_event(self, event: Event | None, *, atomic_dispatch: bool = False) -> None:
        """Attach a stop request that another thread may set during a dispatch."""
        self._emergency_event = event
        self._atomic_emergency = atomic_dispatch
        for simulation in self._simulations.values():
            simulation.set_emergency_event(None if atomic_dispatch else event)
        self._check_emergency()

    def _check_emergency(self, time_ns: int | None = None, *, before_intake: int | None = None) -> None:
        if self.stop_reason is None and self._emergency_event is not None and self._emergency_event.is_set():
            if time_ns is not None:
                self.exchange_ns = max(self.exchange_ns, time_ns)
            self.emergency_stop(at_boundary=self._atomic_emergency, before_intake=before_intake)

    def emergency_stop(self, reason: str = "manual", *, at_boundary: bool = True,
                       before_intake: int | None = None) -> None:
        if self.stop_reason is not None:
            return
        self.stop_reason = reason
        if self.audit_sink is not None:
            self.audit_sink("risk_stop", {"reason": reason, "sequence": self.sequence,
                                         "time_ns": str(self.exchange_ns), "at_boundary": at_boundary,
                                         "dispatch_cursor": self.scheduled_dispatches, "before_intake": before_intake})
        for symbol, simulation in self._simulations.items():
            simulation.sync_clock(self.exchange_ns)
            simulation.emergency_stop()
            self._schedule(symbol, simulation)

    def admit(self, time_ns: int, limit: int) -> bool:
        while self._rate_window and self._rate_window[0] <= time_ns - 1_000_000_000:
            self._rate_window.popleft()
        if len(self._rate_window) >= limit:
            return False
        self._rate_window.append(time_ns)
        return True

    def allows(self, requester: Simulation, side: str, price: int, quantity: int) -> bool:
        limit = requester.config["gross_limit_tl"]
        if not limit:
            return True
        return sum(sim.risk_values((side, price, quantity) if sim is requester else None)[0]
                   for sim in self._simulations.values()) <= limit

    def observe_pnl(self, simulation: Simulation) -> None:
        symbol = canonical_symbol(simulation.symbol)
        pnl = simulation.risk_values()[1]
        self._pnl_total += pnl - self._pnl_by_symbol.get(symbol, 0)
        self._pnl_by_symbol[symbol] = pnl
        if self._pnl_total <= -simulation.config["loss_limit_tl"]:
            self.emergency_stop("loss_limit")

    def external_order(self, command: Command, *, sequence: int | None = None) -> None:
        if self.reply_sink is None:
            raise ValueError("Harici OUCH stratejisi seçilmeli.")
        if sequence is None:
            sequence = self._last_external_sequence + 1
        if type(sequence) is not int or sequence <= self._last_external_sequence:
            raise ValueError("Harici OUCH giriş sırası artmıyor.")
        self._last_external_sequence = sequence
        self._check_emergency(before_intake=sequence)
        if command.kind == "O":
            destinations = [sim for sim in self._simulations.values() if sim.book_id == command.book_id]
        else:
            token = command.previous_token if command.kind == "U" else command.token
            destinations = [sim for sim in self._simulations.values() if sim.has_external_token(token)]
        reason = _REJECT_BOOK if command.kind == "O" else _REJECT_TOKEN
        if command.kind in ("O", "U"):
            if command.token in self._tokens:
                destinations, reason = [], _REJECT_TOKEN
            else:
                if len(self._tokens) >= 1_000_000:
                    raise ValueError("OUCH oturumundaki emir anahtarı sınırı doldu. Replay’i başa al.")
                self._tokens.add(command.token)
        if len(destinations) != 1:
            symbol = self.symbols[0]
            simulation = self._simulations[symbol]
            simulation.reject_external(command, reason, self.exchange_ns)
            self._schedule(symbol, simulation)
            return
        simulation = destinations[0]
        arrival = simulation.external_order(command, self.exchange_ns, sequence=sequence,
                                            arrival_floor=self._last_external_arrival)
        if arrival is not None:
            # One input stream is FIFO even when per-symbol jitter differs.
            self._last_external_arrival = arrival
        self._schedule(canonical_symbol(simulation.symbol), simulation)

    def finish(self) -> None:
        self._arrivals.clear()
        self._scheduled.clear()
        for symbol, simulation in self._simulations.items():
            simulation.sync_clock(self.exchange_ns)
            simulation.finish()
            self._schedule(symbol, simulation)
        self._advance_before(2**64)

    def close_strategies(self) -> None:
        for simulation in self._simulations.values():
            simulation.close_strategy()

    def snapshot(self, symbol: str | None = None) -> dict:
        symbol = self.symbols[0] if symbol is None else canonical_symbol(symbol)
        return self._simulations[symbol].snapshot()

    def snapshot_all(self) -> dict[str, dict]:
        return {symbol: simulation.snapshot() for symbol, simulation in self._simulations.items()}

    def strategy_diagnostics(self, symbol: str) -> dict | None:
        return self._simulations[canonical_symbol(symbol)].strategy_diagnostics()

    def summaries(self) -> list[dict]:
        return [simulation.summary() for simulation in self._simulations.values()]

    def price_shock(self, percent: Fraction) -> dict:
        return price_shock((simulation.marked_position() for simulation in self._simulations.values()), percent)
