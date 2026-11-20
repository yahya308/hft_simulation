"""Deterministic equity-book replay and local, passive limit order simulation."""

from bisect import bisect_left, insort
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from heapq import heapify, heappop, heappush
from math import isfinite

from .ouch import Command, Reply, decode_command, decode_reply, encode_command, encode_reply
from .strategies.two_sided import TwoSidedQuote
from .strategy_api import (Cancel, Enter, MarketEvent, OpenOrder, OrderIntent, OrderUpdate,
                           Replace, Strategy, StrategyError, StrategyView, tick_band, valid_price)


DEFAULT_CONFIG = {
    "quote_size": 10,
    "max_position": 100,
    "quote_interval_ms": 1000,
    "latency_us": 100,
    "spread_ticks": 0,
    "fee_bps": 0.0,
    "enabled": True,
}

_SIDES = ("B", "S")
_CONTINUOUS = frozenset(("P_SUREKLI_ISLEM", "OPEN", "CONTINUOUS", "CONTINUOUS TRADING"))
_REJECT_STALE = -1
_REJECT_PRICE = -2
_REJECT_SIZE = -3
_REJECT_TOKEN = -4
_REJECT_BOOK = -5
_REJECT_PENDING = -6
_SYSTEM_CANCEL = 9
_OWN_RANK_SEQUENCE = 1 << 32  # Source ranks use uint32, and historical messages win time ties.
_REASONS = {
    _REJECT_STALE: "Kotasyon devre dışı veya seans uygun değil",
    _REJECT_PRICE: "Geçersiz fiyat adımı veya pasiflik koşulu",
    _REJECT_SIZE: "Lot veya pozisyon sınırı",
    _REJECT_TOKEN: "Geçersiz veya yinelenen emir kimliği",
    _REJECT_BOOK: "Pay defteri veya emir tarafı uyuşmuyor",
    _REJECT_PENDING: "Bu tarafta bekleyen bir emir isteği var",
    1: "Strateji iptali",
    _SYSTEM_CANCEL: "Seans veya strateji ayarı değişti",
}


def validated_config(settings: dict, base: dict | None = None) -> dict:
    if not isinstance(settings, dict) or set(settings) - DEFAULT_CONFIG.keys():
        raise ValueError("Bilinmeyen strateji ayarı.")
    config = {**(DEFAULT_CONFIG if base is None else base), **settings}
    limits = {"quote_size": (1, 1_000_000), "max_position": (1, 10_000_000),
              "quote_interval_ms": (1, 3_600_000), "latency_us": (0, 10_000_000),
              "spread_ticks": (0, 100)}
    for field, (low, high) in limits.items():
        value = config[field]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{field} için geçerli aralık {low}–{high}.")
    fee = config["fee_bps"]
    if type(fee) not in (int, float) or not isfinite(fee) or not 0 <= fee <= 1000:
        raise ValueError("Komisyon 0–1.000 baz puan arasında olmalı.")
    if type(config["enabled"]) is not bool:
        raise ValueError("Strateji durumu geçersiz.")
    return config


def canonical_symbol(symbol: str) -> str:
    if type(symbol) is not str:
        raise ValueError("Pay kodu ASELS.E biçiminde olmalı.")
    ticker = symbol.removesuffix(".E")
    if not ticker.isascii() or not ticker.isalnum() or ticker != ticker.upper():
        raise ValueError("Pay kodu ASELS.E biçiminde olmalı.")
    return ticker + ".E"


@dataclass(slots=True)
class MarketOrder:
    price: int
    quantity: int
    rank: tuple[int, int, int]


@dataclass(slots=True)
class Level:
    quantity: int = 0
    orders: set[int] = field(default_factory=set)
    priority: list[tuple[tuple[int, int, int], int]] | None = None


class BookSide:
    def __init__(self, side: str, *, ranked: bool = False) -> None:
        self.orders: dict[int, MarketOrder] = {}
        self.levels: dict[int, Level] = {}
        self.ladder: list[int] = []
        self.sign = -1 if side == "B" else 1
        self.ranked = ranked

    def add(self, order_id: int, price: int, quantity: int, rank: tuple[int, int, int]) -> None:
        if order_id in self.orders or quantity < 0:
            raise ValueError("Tekrarlanan veya geçersiz ITCH emri.")
        self.orders[order_id] = MarketOrder(price, quantity, rank)
        if price > 0 and quantity > 0:
            level = self.levels.get(price)
            if level is None:
                level = self.levels[price] = Level(priority=[] if self.ranked else None)
                insort(self.ladder, price * self.sign)
            level.quantity += quantity
            level.orders.add(order_id)
            if level.priority is not None:
                heappush(level.priority, (rank, order_id))
                if len(level.priority) > 2 * len(level.orders) + 64:
                    level.priority[:] = [(self.orders[key].rank, key) for key in level.orders]
                    heapify(level.priority)

    def reduce(self, order_id: int, quantity: int) -> None:
        order = self.orders[order_id]
        if not 0 <= quantity <= order.quantity:
            raise ValueError("ITCH gerçekleşme adedi emir bakiyesini aşıyor.")
        order.quantity -= quantity
        if order.price > 0 and quantity > 0:
            level = self.levels[order.price]
            level.quantity -= quantity
            if order.quantity == 0:
                level.orders.remove(order_id)
            if not level.orders:
                del self.levels[order.price]
                self.ladder.pop(bisect_left(self.ladder, order.price * self.sign))
        if order.quantity == 0:
            del self.orders[order_id]

    def best(self) -> int | None:
        return self.ladder[0] * self.sign if self.ladder else None

    def first(self, level: Level) -> int | None:
        priority = level.priority
        while priority:
            rank, key = priority[0]
            order = self.orders.get(key)
            if order is not None and order.rank == rank:
                return key
            heappop(priority)
        return None

    def remove(self, order_id: int) -> int:
        order = self.orders.get(order_id)
        if order is None:
            return 0
        quantity = order.quantity
        self.reduce(order_id, quantity)
        return quantity

    def remove_level(self, price: int) -> int:
        level = self.levels.pop(price)
        for order_id in level.orders:
            del self.orders[order_id]
        self.ladder.pop(bisect_left(self.ladder, price * self.sign))
        return level.quantity

    def clear(self) -> None:
        self.orders.clear()
        self.levels.clear()
        self.ladder.clear()


@dataclass(slots=True)
class Quote:
    root_token: str
    token: str
    order_id: int
    side: str
    price: int
    remaining: int
    executed: int = 0


@dataclass(slots=True)
class ScheduledCommand:
    arrival_ns: int
    side: str
    generation: int
    request_sequence: int
    frame: bytes
    order_id: int | None
    origin_sequence: int


class Portfolio:
    def __init__(self) -> None:
        self.position = 0
        self.average = Fraction(0)
        self.cash = Fraction(0)
        self.realized = Fraction(0)

    def fill(self, side: str, price: int, quantity: int, fee: Fraction) -> None:
        change = quantity if side == "B" else -quantity
        old = self.position
        new = old + change
        self.cash -= change * price + fee
        self.realized -= fee
        if old * change > 0:
            self.average = (self.average * abs(old) + price * quantity) / abs(new)
        elif old * change < 0:
            closed = min(abs(old), quantity)
            self.realized += closed * (price - self.average) * (1 if old > 0 else -1)
            if old * new < 0:
                self.average = Fraction(price)
        elif not old:
            self.average = Fraction(price)
        if not new:
            self.average = Fraction(0)
        self.position = new


class Simulation:
    """Each call applies one recorded ITCH event in sequence order."""

    def __init__(self, config: dict | None = None, *, strategy: Strategy | None = None,
                 symbol: str = "ASELS") -> None:
        self._expected_symbol = canonical_symbol(symbol)
        self.config = validated_config({} if config is None else config)
        self._fee = Fraction(str(self.config["fee_bps"])) / 10_000
        self._legacy_parameters = strategy is None
        self._strategy = self._configured_quotes() if strategy is None else strategy
        # The built-in decision is a known no-op between quote intervals.
        self._coalesced_quotes = type(self._strategy) is TwoSidedQuote
        self._updates: list[OrderUpdate] | None = None if self._coalesced_quotes else []
        self._reference_book = {side: BookSide(side) for side in _SIDES}
        self._book = {side: BookSide(side, ranked=True) for side in _SIDES}
        self._displaced_sides: set[str] = set()
        self._seen_matches: set[int] = set()
        self._matching_suspended = False
        self._matching_audit = {
            "observed_events": 0, "observed_quantity": 0, "strategy_quantity": 0,
            "historical_quantity": 0, "unallocated_quantity": 0, "skipped_events": 0,
            "skipped_quantity": 0, "crossing_events": 0, "reset_events": 0,
            "reconcile_events": 0, "reconciled_quantity": 0,
        }
        self._matching_journal: deque[dict] = deque(maxlen=60)
        self._ticks: dict[tuple[int, int], int] = {}
        self._tick_bands: tuple[tuple[int, int, int], ...] = ()
        self._quotes: dict[str, Quote] = {}
        self._commands: deque[ScheduledCommand] = deque()
        self._pending: dict[str, int] = {}
        self._generation = 0
        self._last_arrival = 0
        self._token_id = 0
        self._order_id = 0
        self._sent = 0
        self._request_sequence = 0
        self._received = 0
        self._journal: deque[dict] = deque(maxlen=100)
        self._trades: deque[dict] = deque(maxlen=60)
        self._fills = 0
        self._portfolio = Portfolio()
        self._last_mark: Fraction | None = None
        self._session_ended = False
        self.symbol = symbol
        self.book_id: int | None = None
        self.sequence = 0
        self.exchange_ns = 0
        self._source_port: int | None = None
        self._source_sequence: int | None = None
        self.price_decimals = 0
        self._scale = 1
        self.round_lot = 0
        self.phase = ""

    def _configured_quotes(self) -> TwoSidedQuote:
        return TwoSidedQuote(self.config["quote_size"], self.config["quote_interval_ms"],
                             self.config["spread_ticks"])

    def configure(self, settings: dict) -> None:
        new = validated_config(settings, self.config)
        if self.config != new:
            self.config = new
            self._fee = Fraction(str(new["fee_bps"])) / 10_000
            if self._legacy_parameters:
                self._strategy = self._configured_quotes()
            self._withdraw()

    def set_enabled(self, enabled: bool) -> None:
        self.configure({"enabled": enabled})

    def finish(self) -> None:
        if self._session_ended:
            return
        self._session_ended = True
        self._withdraw()
        while self._commands:
            pending = self._commands.popleft()
            command = decode_command(pending.frame)
            self._reply(Reply("J", self.exchange_ns, command.token, reason=_REJECT_STALE),
                        side=pending.side, request=pending.request_sequence, related_order_id=pending.order_id)

    def sync_clock(self, time_ns: int) -> None:
        """Move the local clock without a strategy callback or a market event."""
        self.exchange_ns = max(self.exchange_ns, time_ns)

    def next_arrival(self) -> tuple[int, int, int] | None:
        if not self._commands:
            return None
        pending = self._commands[0]
        return pending.arrival_ns, pending.origin_sequence, pending.request_sequence

    def receive_next(self) -> None:
        """Deliver the FIFO head at its recorded arrival time."""
        pending = self._commands.popleft()
        self.sync_clock(pending.arrival_ns)
        self._receive(pending)

    def _active(self) -> bool:
        return (self.config["enabled"] and self.phase in _CONTINUOUS and not self._session_ended
                and not self._matching_suspended)

    def _price(self, raw: int | Fraction | None) -> float | None:
        return None if raw is None else float(raw / self._scale)

    def _token(self) -> str:
        self._token_id += 1
        return f"S{self._token_id:013d}"

    def _log(self, direction: str, kind: str, token: str, time_ns: int, frame: bytes, *,
             side: str = "", price: int = 0, quantity: int | None = None, reason: int = 0,
             previous_token: str = "", request_sequence: int | None = None) -> None:
        self._journal.append({
            "sequence": self._sent + self._received, "time_ns": str(time_ns),
            "direction": direction, "type": kind, "token": token, "side": side or None,
            "price": self._price(price) if price else None, "quantity": quantity,
            "request_sequence": request_sequence,
            "ouch_sequence": self._received if direction == "receive" else None,
            "reason": reason or None, "reason_text": _REASONS.get(reason),
            "previous_token": previous_token or None, "frame_hex": frame.hex(),
        })

    def _send(self, command: Command, side: str, order_id: int | None = None) -> None:
        frame = encode_command(command)
        self._sent += 1
        self._request_sequence += 1
        request = self._request_sequence
        arrival = max(self._last_arrival, self.exchange_ns + self.config["latency_us"] * 1000)
        self._last_arrival = arrival
        self._commands.append(ScheduledCommand(arrival, side, self._generation, request, frame, order_id,
                                               self.sequence))
        self._pending[side] = request
        self._log("send", command.kind, command.token, self.exchange_ns, frame, side=side,
                  price=command.price, quantity=command.quantity or None,
                  previous_token=command.previous_token, request_sequence=request)

    def _reply(self, reply: Reply, *, side: str = "", request: int | None = None,
               canceled_quantity: int | None = None, related_order_id: int | None = None) -> None:
        frame = encode_reply(reply)
        decoded = decode_reply(frame)
        self._received += 1  # SoupBinTCP S packets carry an implied sequence.
        quantity = canceled_quantity if decoded.kind == "C" else decoded.quantity or None
        self._log("receive", decoded.kind, decoded.token, decoded.time_ns, frame,
                  side=decoded.side or side, price=decoded.price, quantity=quantity,
                  reason=decoded.reason, previous_token=decoded.previous_token, request_sequence=request)
        if self._updates is not None:
            self._updates.append(OrderUpdate(
                decoded.kind, self._received, decoded.time_ns, decoded.side or side or None,
                reply.order_id or related_order_id, request, decoded.token, decoded.price or None,
                canceled_quantity if decoded.kind == "C" else None if decoded.kind == "J" else decoded.quantity,
                decoded.reason or None,
            ))

    def _cancel_quote(self, quote: Quote, time_ns: int, reason: int, request: int | None = None) -> None:
        self._book[quote.side].remove(-quote.order_id)
        del self._quotes[quote.side]
        self._reply(Reply("C", time_ns, quote.token, self.book_id, quote.side,
                          quote.order_id, reason=reason), request=request, canceled_quantity=quote.remaining)

    def _withdraw(self) -> None:
        self._generation += 1
        self._pending.clear()
        for quote in tuple(self._quotes.values()):
            self._cancel_quote(quote, self.exchange_ns, _SYSTEM_CANCEL)

    def _tick(self, price: int) -> tuple[int, int] | None:
        return tick_band(price, self._tick_bands)

    def _valid_price(self, price: int) -> bool:
        return valid_price(price, self._tick_bands)

    def _capacity(self, side: str) -> int:
        return self.config["max_position"] + self._book[side].sign * self._portfolio.position

    def _strategy_view(self, bid: int | None, ask: int | None) -> StrategyView:
        orders = tuple(OpenOrder(quote.order_id, quote.side, quote.price, quote.remaining, quote.executed)
                       for quote in self._quotes.values())
        return StrategyView(
            self.symbol, self.phase, self.exchange_ns, self.sequence, bid, ask,
            self._book["B"].levels[bid].quantity if bid is not None else 0,
            self._book["S"].levels[ask].quantity if ask is not None else 0,
            self.price_decimals, self.round_lot, self._portfolio.position,
            max(0, self._capacity("B")), max(0, self._capacity("S")),
            orders, tuple(self._pending), self._generation, self._tick_bands,
        )

    def _decide(self, kind: str, args: list, bid: int | None, ask: int | None) -> None:
        if not self._active() or (self._coalesced_quotes and
                                  not self._strategy._due(self.exchange_ns, self._generation)):
            return
        event = MarketEvent(kind, self.sequence, self.exchange_ns, tuple(args),
                            self._source_port, self._source_sequence)
        view = self._strategy_view(bid, ask)
        updates = tuple(self._updates) if self._updates else ()
        if self._updates is not None:
            self._updates.clear()
        try:
            intents = self._strategy.on_event(event, view, updates)
            self._validate_intents(intents)
        except Exception as error:
            self.finish()
            raise StrategyError(self.sequence, str(error) or type(error).__name__) from error
        for intent in intents:
            self._route_intent(intent)

    @staticmethod
    def _validate_intents(intents: tuple[OrderIntent, ...]) -> None:
        if type(intents) is not tuple:
            raise TypeError("Strateji bir emir kararı demeti döndürmeli.")
        for intent in intents:
            if type(intent) is Enter:
                valid = (type(intent.side) is str and intent.side in _SIDES and
                         type(intent.price) is int and type(intent.quantity) is int)
            elif type(intent) is Replace:
                valid = (type(intent.order_id) is int and type(intent.price) is int and
                         type(intent.remaining_quantity) is int)
            elif type(intent) is Cancel:
                valid = type(intent.order_id) is int
            else:
                valid = False
            if not valid:
                raise TypeError("Enter, Replace veya Cancel kararındaki alanlar geçersiz.")

    def _reject_intent(self, reason: int, *, side: str = "", order_id: int | None = None) -> None:
        self._request_sequence += 1
        self._reply(Reply("J", self.exchange_ns, self._token(), reason=reason), side=side,
                    request=self._request_sequence, related_order_id=order_id)

    def _route_intent(self, intent: OrderIntent) -> None:
        if isinstance(intent, Enter):
            side = intent.side
            if side in self._pending:
                self._reject_intent(_REJECT_PENDING, side=side)
            elif self.book_id is None:
                self._reject_intent(_REJECT_BOOK, side=side)
            elif not 0 < intent.price < 2**31:
                self._reject_intent(_REJECT_PRICE, side=side)
            elif not 0 <= intent.quantity < 2**64:
                self._reject_intent(_REJECT_SIZE, side=side)
            else:
                self._send(Command("O", self._token(), self.book_id, side, intent.price, intent.quantity), side)
            return

        order_id = intent.order_id
        live = next((quote for quote in self._quotes.values() if quote.order_id == order_id), None)
        if live is None:
            self._reject_intent(_REJECT_TOKEN, order_id=order_id)
        elif live.side in self._pending:
            self._reject_intent(_REJECT_PENDING, side=live.side, order_id=order_id)
        elif isinstance(intent, Cancel):
            self._send(Command("X", live.root_token), live.side, order_id)
        elif not 0 < intent.price < 2**31:
            self._reject_intent(_REJECT_PRICE, side=live.side, order_id=order_id)
        elif intent.remaining_quantity < 0 or intent.remaining_quantity + live.executed >= 2**64:
            self._reject_intent(_REJECT_SIZE, side=live.side, order_id=order_id)
        else:
            self._send(Command("U", self._token(), price=intent.price,
                               quantity=intent.remaining_quantity + live.executed,
                               previous_token=live.root_token), live.side, order_id)

    def _rest_quote(self, quote: Quote, pending: ScheduledCommand) -> None:
        rank = (pending.arrival_ns, _OWN_RANK_SEQUENCE, pending.request_sequence)
        self._book[quote.side].add(-quote.order_id, quote.price, quote.remaining, rank)

    def _validate_quote(self, side: str, price: int, quantity: int) -> int:
        bid, ask = (self._book[entry].best() for entry in _SIDES)
        opposite = self._quotes.get("S" if side == "B" else "B")
        if not self._valid_price(price) or bid is None or ask is None or bid >= ask:
            return _REJECT_PRICE
        if side == "B" and (price >= ask or (opposite and price >= opposite.price)):
            return _REJECT_PRICE
        if side == "S" and (price <= bid or (opposite and price <= opposite.price)):
            return _REJECT_PRICE
        if not self.round_lot or not 0 < quantity <= self._capacity(side) or quantity % self.round_lot:
            return _REJECT_SIZE
        return 0

    def _receive(self, pending: ScheduledCommand) -> None:
        command = decode_command(pending.frame)
        side, time = pending.side, pending.arrival_ns
        if self._pending.get(side) == pending.request_sequence:
            del self._pending[side]
        live = self._quotes.get(side)
        reason = 0
        if pending.generation != self._generation or not self._active():
            reason = _REJECT_STALE
        elif command.kind == "O":
            if command.book_id != self.book_id or command.side != side:
                reason = _REJECT_BOOK
            elif live is not None:
                reason = _REJECT_TOKEN
            else:
                reason = self._validate_quote(side, command.price, command.quantity)
                if not reason:
                    self._order_id += 1
                    live = Quote(command.token, command.token, self._order_id, side,
                                 command.price, command.quantity)
                    self._rest_quote(live, pending)
                    self._quotes[side] = live
                    self._reply(Reply("A", time, live.token, self.book_id, side, live.order_id,
                                      live.price, live.remaining, pre_trade_quantity=live.remaining),
                                request=pending.request_sequence)
        elif live is None or live.root_token != (command.token if command.kind == "X" else command.previous_token):
            reason = _REJECT_TOKEN
        elif command.kind == "X":
            self._cancel_quote(live, time, 1, pending.request_sequence)
        else:
            remaining = max(0, command.quantity - live.executed)
            price = command.price or live.price
            reason = self._validate_quote(side, price, remaining) if remaining else 0
            if not reason:
                retain = price == live.price and remaining <= live.remaining
                previous = live.token
                book = self._book[side]
                if retain:
                    book.reduce(-live.order_id, live.remaining - remaining)
                else:
                    book.remove(-live.order_id)
                live.token, live.price, live.remaining = command.token, price, remaining
                if not retain and remaining:
                    self._rest_quote(live, pending)
                if not remaining:
                    del self._quotes[side]
                self._reply(Reply("U", time, live.token, self.book_id, side, live.order_id, live.price,
                                  remaining, previous, pre_trade_quantity=remaining),
                            request=pending.request_sequence)
        if reason:
            self._reply(Reply("J", time, command.token, reason=reason), side=side,
                        request=pending.request_sequence, related_order_id=pending.order_id)

    def _fill(self, quote: Quote, quantity: int, source_kind: str, match_id: int | None) -> int:
        quantity = min(quote.remaining, quantity, max(0, self._capacity(quote.side)))
        if not quantity:
            return 0
        fee = quantity * quote.price * self._fee
        self._portfolio.fill(quote.side, quote.price, quantity, fee)
        quote.remaining -= quantity
        quote.executed += quantity
        self._book[quote.side].reduce(-quote.order_id, quantity)
        self._fills += 1
        self._reply(Reply("E", self.exchange_ns, quote.token, self.book_id, quote.side,
                          quote.order_id, quote.price, quantity, match_id=self._fills), side=quote.side)
        self._trades.append({
            "sequence": self._fills, "time_ns": str(self.exchange_ns), "side": quote.side,
            "price": self._price(quote.price), "quantity": quantity, "token": quote.token,
            "fee": self._price(fee), "source_sequence": self.sequence, "source_kind": source_kind,
            "source_match_id": str(match_id) if match_id is not None else None,
        })
        if self._source_port is not None:
            self._trades[-1].update(source_port=self._source_port, source_mold_sequence=self._source_sequence)
        if not quote.remaining:
            del self._quotes[quote.side]
        return quantity

    def _audit(self, event: str, **fields: object) -> None:
        self._matching_journal.append({"event": event, "source_sequence": self.sequence,
                                       "time_ns": str(self.exchange_ns), **fields})
        if self._source_port is not None:
            self._matching_journal[-1].update(source_port=self._source_port,
                                              source_mold_sequence=self._source_sequence)

    def _match_budget(self, side: str, limit: int, quantity: int, kind: str, match_id: int | None,
                      source_order_id: int) -> tuple[int, int, int, int]:
        book = self._book[side]
        live = self._quotes.get(side)
        own = historical = source = 0
        index = 0
        signed_limit = limit * book.sign
        while quantity and index < len(book.ladder):
            key = book.ladder[index]
            if key > signed_limit:
                break
            price = key * book.sign
            level = book.levels[price]
            deferred = None
            while quantity and level.orders:
                order_id = book.first(level)
                if order_id is None:
                    break
                order = book.orders[order_id]
                amount = min(quantity, order.quantity)
                if order_id < 0:
                    amount = self._fill(live, amount, kind, match_id)
                    own += amount
                    if order_id in book.orders and amount < quantity:
                        # At the risk limit this resting order cannot absorb the residual budget.
                        deferred = heappop(level.priority)
                else:
                    book.reduce(order_id, amount)
                    historical += amount
                    if order_id == source_order_id:
                        source += amount
                quantity -= amount
            if deferred is not None:
                heappush(level.priority, deferred)
            if price in book.levels:
                index += 1
        return own, historical, quantity, source

    def _shadow_execution(self, order_id: int, side: str, order: MarketOrder, quantity: int,
                          kind: str, metadata: list) -> None:
        book = self._book[side]
        match_id = metadata[0] if metadata and type(metadata[0]) is int and metadata[0] > 0 else None
        limit = order.price
        reason = None
        if self.phase not in _CONTINUOUS or self._session_ended:
            reason = "outside_continuous"
        elif self._matching_suspended:
            reason = "crossed_shadow"
        elif not 0 < order.price < 2**31 or not quantity:
            reason = "not_visible"
        elif kind == "C":
            if len(metadata) != 4 or match_id is None:
                reason = "missing_price_evidence"
            else:
                _, limit, at_cross, printable = metadata
                if at_cross != "N" or printable != "Y":
                    reason = "not_continuous_print"
                elif (type(limit) is not int or not 0 < limit < 2**31 or
                      limit * book.sign < order.price * book.sign):
                    reason = "inconsistent_trade_price"
        if reason is None and match_id is not None:
            if match_id in self._seen_matches:
                reason = "duplicate_match"
            else:
                self._seen_matches.add(match_id)

        audit = self._matching_audit
        if reason is not None:
            shadow_order = book.orders.get(order_id)
            if shadow_order is not None:
                book.reduce(order_id, min(quantity, shadow_order.quantity))
            audit["skipped_events"] += 1
            audit["skipped_quantity"] += quantity
            self._audit("skipped", source_kind=kind, side=side, reason=reason, quantity=quantity,
                        match_id=str(match_id) if match_id is not None else None)
            return

        own_quote = self._quotes.get(side)
        own_order = None if own_quote is None else book.orders[-own_quote.order_id]
        # Until this side diverges, the exchange's target ID is authoritative when our order
        # has no price/time priority over it. A replay with no strategy mirrors the input.
        can_displace = own_order is not None and (
            own_order.price * book.sign, own_order.rank
        ) < (order.price * book.sign, order.rank)
        if side in self._displaced_sides or can_displace:
            own, historical, unallocated, source = self._match_budget(side, limit, quantity, kind,
                                                                       match_id, order_id)
            if source != quantity:
                self._displaced_sides.add(side)
        else:
            book.reduce(order_id, quantity)
            own, historical, unallocated, source = 0, quantity, 0, quantity
        audit["observed_events"] += 1
        audit["observed_quantity"] += quantity
        audit["strategy_quantity"] += own
        audit["historical_quantity"] += historical
        audit["unallocated_quantity"] += unallocated
        self._audit("allocation", source_kind=kind, side=side, source_order_id=str(order_id),
                    match_id=str(match_id) if match_id is not None else None, price=self._price(limit),
                    observed_quantity=quantity, strategy_quantity=own, historical_quantity=historical,
                    unallocated_quantity=unallocated, source_order_quantity=source)

    def _apply_order(self, kind: str, args: list) -> None:
        order_id, side = args[:2]
        reference = self._reference_book[side]
        book = self._book[side]
        if kind == "A":
            price, quantity, ranking_time, ranking_sequence = args[2:]
            if type(order_id) is not int or order_id < 0:
                raise ValueError("Geçersiz ITCH emir kimliği.")
            rank = (ranking_time, ranking_sequence, self.sequence)
            reference.add(order_id, price, quantity, rank)
            if order_id in book.orders:
                previous = book.remove(order_id)
                self._audit("lifecycle", side=side, source_order_id=str(order_id), quantity=previous)
            book.add(order_id, price, quantity, rank)
            return

        order = reference.orders.get(order_id)
        if order is None:
            raise ValueError(f"ITCH emri bulunamadı ({kind}, sıra {self.sequence}).")
        quantity = order.quantity if kind == "D" else args[2]
        if not 0 <= quantity <= order.quantity:
            raise ValueError("ITCH gerçekleşme adedi emir bakiyesini aşıyor.")
        if kind == "D":
            removed = book.remove(order_id)
            if removed != quantity:
                self._audit("cancel", side=side, source_order_id=str(order_id),
                            reference_quantity=quantity, shadow_quantity=removed)
        else:
            self._shadow_execution(order_id, side, order, quantity, kind, args[3:])
        reference.reduce(order_id, quantity)

    def _reset_shadow(self, reason: str) -> None:
        if self._displaced_sides or self._matching_suspended:
            for side in _SIDES:
                book = self._book[side]
                book.clear()
                for order_id, order in self._reference_book[side].orders.items():
                    book.add(order_id, order.price, order.quantity, order.rank)
        self._displaced_sides.clear()
        self._matching_suspended = False
        self._matching_audit["reset_events"] += 1
        self._audit("reset", reason=reason)

    def _reconcile_crossing(self, reference_bid: int | None,
                            reference_ask: int | None) -> tuple[int | None, int | None]:
        buy, sell = self._book["B"], self._book["S"]
        bid, ask = buy.best(), sell.best()
        removed_buy = removed_sell = 0
        while bid is not None and ask is not None and bid >= ask:
            # At least one of these levels is beyond the uncrossed reference book.
            # Once quotes are withdrawn it can only contain displaced historical orders.
            if reference_bid is None or bid > reference_bid:
                removed_buy += buy.remove_level(bid)
            if reference_ask is None or ask < reference_ask:
                removed_sell += sell.remove_level(ask)
            bid, ask = buy.best(), sell.best()
        self._matching_audit["reconcile_events"] += 1
        self._matching_audit["reconciled_quantity"] += removed_buy + removed_sell
        self._audit("reconciliation", reason="crossed_historical_residual",
                    buy_quantity=removed_buy, sell_quantity=removed_sell)
        return bid, ask

    def _shadow_bbo(self, reference_bid: int | None,
                    reference_ask: int | None) -> tuple[int | None, int | None, bool]:
        buy, sell = self._book["B"], self._book["S"]
        bid, ask = buy.best(), sell.best()
        if self.phase not in _CONTINUOUS or self._session_ended:
            return bid, ask, True
        reference_crossed = (reference_bid is not None and reference_ask is not None
                             and reference_bid >= reference_ask)
        if reference_crossed or (bid is not None and ask is not None and bid >= ask):
            if not self._matching_suspended:
                self._matching_suspended = True
                self._matching_audit["crossing_events"] += 1
                reason = "crossed_reference" if reference_crossed else "crossed_shadow"
                self._audit("suspended", reason=reason, bid=self._price(bid), ask=self._price(ask))
                self._withdraw()
                bid, ask = buy.best(), sell.best()
            if not reference_crossed:
                if bid is not None and ask is not None and bid >= ask:
                    bid, ask = self._reconcile_crossing(reference_bid, reference_ask)
                self._matching_suspended = False
                self._audit("resumed", reason="uncrossed_shadow")
            # The trigger supplies no verified aggressor. Resume decisions on the next event.
            return bid, ask, False
        if self._matching_suspended:
            self._matching_suspended = False
            self._audit("resumed", reason="uncrossed_shadow")
        return bid, ask, True

    def apply(self, event: list, *, source_port: int | None = None, source_sequence: int | None = None) -> None:
        kind, sequence, time, *args = event
        if sequence <= self.sequence:
            raise ValueError("ITCH sırası artmıyor.")
        time = max(time, self.exchange_ns)
        # Historical messages at the same timestamp win the tie. Process pending
        # arrivals only once the next observed timestamp is strictly later.
        while self._commands and self._commands[0].arrival_ns < time:
            self.receive_next()
        self.sequence, self.exchange_ns = sequence, time
        self._source_port, self._source_sequence = source_port, source_sequence
        if kind == "R":
            book_id, symbol, decimals, lot = args
            if (canonical_symbol(symbol) != self._expected_symbol or self.book_id not in (None, book_id)
                    or not 0 <= decimals <= 9):
                raise ValueError(f"Bu simülasyon yalnızca {self._expected_symbol} defterini kabul eder.")
            self.book_id, self.symbol, self.price_decimals, self.round_lot = book_id, symbol, decimals, lot
            self._scale = 10**decimals
        elif kind == "L":
            low, high, size = args
            if size <= 0:
                raise ValueError("ITCH fiyat adımı geçersiz.")
            self._ticks[low, high] = size
            self._tick_bands = tuple((low, high, size) for (low, high), size in self._ticks.items())
        elif kind == "O":
            was_continuous = self.phase in _CONTINUOUS
            self.phase = args[0].strip().upper()
            if was_continuous and self.phase not in _CONTINUOUS:
                self._withdraw()
                self._reset_shadow("phase_change")
        elif kind in ("A", "D", "E", "C") and self.book_id is not None:
            self._apply_order(kind, args)
        elif kind == "Y":
            self._withdraw()
            for side in self._reference_book.values():
                side.clear()
            for side in self._book.values():
                side.clear()
            self._reset_shadow("book_flush")
        elif kind == "S" and args[0] == "C":
            self.finish()
        reference_bid, reference_ask = self._reference_book["B"].best(), self._reference_book["S"].best()
        if reference_bid is not None and reference_ask is not None and reference_bid < reference_ask:
            self._last_mark = Fraction(reference_bid + reference_ask, 2)
        bid, ask, can_decide = self._shadow_bbo(reference_bid, reference_ask)
        if can_decide:
            self._decide(kind, args, bid, ask)

    def _levels(self, book: BookSide, quote: Quote | None = None) -> list[dict]:
        rows = []
        for key in book.ladder[:20]:
            price = key * book.sign
            level = book.levels[price]
            row = {"price": self._price(price), "quantity": level.quantity, "orders": len(level.orders)}
            if book.ranked:
                row["own_quantity"] = quote.remaining if quote is not None and price == quote.price else 0
            rows.append(row)
        return rows

    def _ahead_quantity(self, quote: Quote) -> int:
        book = self._book[quote.side]
        own_rank = book.orders[-quote.order_id].rank
        level = book.levels[quote.price]
        better = bisect_left(book.ladder, quote.price * book.sign)
        return (sum(book.levels[key * book.sign].quantity for key in book.ladder[:better])
                + sum(book.orders[key].quantity for key in level.orders if book.orders[key].rank < own_rank))

    def _unrealized(self) -> int | Fraction:
        portfolio = self._portfolio
        return (self._last_mark - portfolio.average) * portfolio.position if self._last_mark is not None else 0

    @staticmethod
    def _midpoint(bid: int | None, ask: int | None) -> Fraction | None:
        return Fraction(bid + ask, 2) if bid is not None and ask is not None and bid < ask else None

    def summary(self) -> dict:
        reference_middle = self._midpoint(self._reference_book["B"].best(), self._reference_book["S"].best())
        return {"symbol": self.symbol, "book_id": self.book_id, "position": self._portfolio.position,
                "total_pnl": self._price(self._portfolio.realized + self._unrealized()),
                "fills": self._fills, "reference_mid_price": self._price(reference_middle)}

    def snapshot(self) -> dict:
        buy, sell = self._book["B"], self._book["S"]
        reference_buy, reference_sell = self._reference_book["B"], self._reference_book["S"]
        bid, ask = buy.best(), sell.best()
        reference_bid, reference_ask = reference_buy.best(), reference_sell.best()
        live_bid, live_ask = (self._quotes.get(side) for side in _SIDES)
        portfolio = self._portfolio
        unrealized = self._unrealized()
        tick = self._tick(best) if (best := bid if bid is not None else ask) is not None else None
        middle = self._midpoint(bid, ask)
        reference_middle = self._midpoint(reference_bid, reference_ask)
        return {
            "symbol": self.symbol, "book_id": self.book_id, "sequence": self.sequence,
            "exchange_ns": str(self.exchange_ns), "phase": self.phase or None,
            "price_decimals": self.price_decimals, "tick_size": self._price(tick[1]) if tick else None,
            "round_lot": self.round_lot or None, "bids": self._levels(buy, live_bid),
            "asks": self._levels(sell, live_ask),
            "best_bid": self._price(bid), "best_ask": self._price(ask), "mid_price": self._price(middle),
            "reference_bids": self._levels(reference_buy), "reference_asks": self._levels(reference_sell),
            "reference_best_bid": self._price(reference_bid), "reference_best_ask": self._price(reference_ask),
            "reference_mid_price": self._price(reference_middle),
            "mark_price": self._price(self._last_mark),
            "quote_bid": self._price(live_bid.price) if live_bid else None,
            "quote_ask": self._price(live_ask.price) if live_ask else None,
            "quote_bid_quantity": live_bid.remaining if live_bid else None,
            "quote_ask_quantity": live_ask.remaining if live_ask else None,
            "bid_ahead": self._ahead_quantity(live_bid) if live_bid else None,
            "ask_ahead": self._ahead_quantity(live_ask) if live_ask else None,
            "position": portfolio.position, "average_price": self._price(portfolio.average) if portfolio.position else None,
            "cash": self._price(portfolio.cash), "realized_pnl": self._price(portfolio.realized),
            "unrealized_pnl": self._price(unrealized), "total_pnl": self._price(portfolio.realized + unrealized),
            "fills": self._fills, "ouch_sent": self._sent, "ouch_received": self._received,
            "order_log": list(self._journal), "trades": list(self._trades),
            "matching_audit": {**self._matching_audit, "suspended": self._matching_suspended,
                               "journal": list(self._matching_journal)},
            "strategy_enabled": self.config["enabled"], "strategy_active": self._active(), "config": dict(self.config),
        }
