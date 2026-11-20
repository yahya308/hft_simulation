"""Route ordered ITCH rows to independent equity simulations."""

from collections.abc import Callable, Mapping
from heapq import heapify, heappop, heappush

from .simulation import Simulation, canonical_symbol
from .strategy_api import Strategy, StrategyError


class MultiSimulation:
    def __init__(self, configs: Mapping[str, dict], *,
                 strategy_factory: Callable[[str], Strategy | None] | None = None,
                 sources: Mapping[str, int] | None = None) -> None:
        if not configs:
            raise ValueError("Replay için en az bir pay seçilmeli.")
        self._simulations: dict[str, Simulation] = {}
        strategy_instances = set()
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
        self.symbols = tuple(self._simulations)
        self.sequence = 0
        self.exchange_ns = 0
        self._sources = self._validate_sources(sources)
        self._symbols_by_source = self._group_sources()
        self._book_symbols: dict[tuple[int | None, int], str] = {}
        self._source_sequences: dict[int | None, int] = {}
        self._arrivals: list[tuple[int, int, int, str]] = []
        self._scheduled: set[str] = set()

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
        if symbol not in self._scheduled and (arrival := simulation.next_arrival()) is not None:
            heappush(self._arrivals, (*arrival, symbol))
            self._scheduled.add(symbol)

    def _advance_before(self, time_ns: int) -> None:
        # The feed, including every book at an identical timestamp, beats OUCH arrival ties.
        while self._arrivals and self._arrivals[0][0] < time_ns:
            _, _, _, symbol = heappop(self._arrivals)
            self._scheduled.remove(symbol)
            simulation = self._simulations[symbol]
            simulation.receive_next()
            self._schedule(symbol, simulation)

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
        self._arrivals[:] = [item for item in self._arrivals if item[3] not in affected]
        heapify(self._arrivals)
        self._scheduled.difference_update(affected)

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
        for simulation in self._simulations.values():
            simulation.sync_clock(self.exchange_ns)
            simulation.set_enabled(enabled)

    def finish(self) -> None:
        self._arrivals.clear()
        self._scheduled.clear()
        for simulation in self._simulations.values():
            simulation.sync_clock(self.exchange_ns)
            simulation.finish()

    def snapshot(self, symbol: str | None = None) -> dict:
        symbol = self.symbols[0] if symbol is None else canonical_symbol(symbol)
        return self._simulations[symbol].snapshot()

    def snapshot_all(self) -> dict[str, dict]:
        return {symbol: simulation.snapshot() for symbol, simulation in self._simulations.items()}

    def summaries(self) -> list[dict]:
        return [simulation.summary() for simulation in self._simulations.values()]
