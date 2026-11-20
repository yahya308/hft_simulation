"""Binary BISTECH OUCH Day limit subset over SoupBinTCP U/S data frames."""

from dataclasses import dataclass
from struct import Struct


# BISTECH OUCH, sections 4.1.1–4.1.3 and 4.2.1–4.2.5.
# https://borsaistanbul.com/files/bistech-ouch-protocol-specification.pdf
_SOUP = Struct("!Hc")
_ENTER = Struct("!c14sIcQiBB16s15s32sQBBBB3s2x")
_REPLACE = Struct("!c14s14sQiB16s15s32sQB8x")
_CANCEL = Struct("!c14s")
_ACCEPTED = Struct("!cQ14sIcQQiBB16sB15s32sQQBBBB3s")
_REJECTED = Struct("!cQ14si")
_REPLACED = Struct("!cQ14s14sIcQQiBB16sB15s32sQQB")
_CANCELED = Struct("!cQ14sIcQB")
_EXECUTED = Struct("!cQ14sIQi12sB16x")

_ACCOUNT = b"ASELS-SIM".ljust(32)
_CLIENT = b" " * 16
_CUSTOMER = b" " * 15
_CLIENT_CATEGORY = 2


@dataclass(frozen=True, slots=True)
class Command:
    kind: str
    token: str
    book_id: int = 0
    side: str = ""
    price: int = 0
    quantity: int = 0
    previous_token: str = ""


@dataclass(frozen=True, slots=True)
class Reply:
    kind: str
    time_ns: int
    token: str
    book_id: int = 0
    side: str = ""
    order_id: int = 0
    price: int = 0
    quantity: int = 0
    previous_token: str = ""
    reason: int = 0
    match_id: int = 0
    pre_trade_quantity: int = 0


def _token(token: str) -> bytes:
    raw = token.encode("ascii")
    if not 0 < len(raw) <= 14 or any(not 33 <= byte <= 126 for byte in raw):
        raise ValueError("invalid OUCH order token")
    return raw.ljust(14)


def _read_token(raw: bytes) -> str:
    token = raw.rstrip(b" ").decode("ascii")
    _token(token)
    return token


def data_frame(payload: bytes, kind: bytes) -> bytes:
    if kind not in (b"U", b"S") or not 0 < len(payload) < 65_535:
        raise ValueError("invalid SoupBinTCP data frame")
    return _SOUP.pack(len(payload) + 1, kind) + payload


def _payload(frame: bytes, expected_kind: bytes) -> bytes:
    if len(frame) <= _SOUP.size:
        raise ValueError("truncated SoupBinTCP data frame")
    length, kind = _SOUP.unpack_from(frame)
    if length != len(frame) - 2 or kind != expected_kind:
        raise ValueError("invalid SoupBinTCP data frame length or type")
    return frame[_SOUP.size:]


def encode_command(command: Command) -> bytes:
    token = _token(command.token)
    if command.kind == "O":
        if command.side not in ("B", "S"):
            raise ValueError("unsupported OUCH side")
        payload = _ENTER.pack(b"O", token, command.book_id, command.side.encode("ascii"),
                              command.quantity, command.price, 0, 0, _CLIENT, _CUSTOMER,
                              _ACCOUNT, 0, _CLIENT_CATEGORY, 0, 0, 0, b"   ")
    elif command.kind == "U":
        payload = _REPLACE.pack(b"U", _token(command.previous_token), token,
                                command.quantity, command.price, 0, b"\0" + _CLIENT[1:],
                                b"\0" + _CUSTOMER[1:], b"\0" + b" " * 31,
                                0, 0)
    elif command.kind == "X":
        payload = _CANCEL.pack(b"X", token)
    else:
        raise ValueError("unknown OUCH command type")
    return data_frame(payload, b"U")


def decode_command(frame: bytes) -> Command:
    payload = _payload(frame, b"U")
    kind = payload[:1]
    if kind == b"O" and len(payload) == _ENTER.size:
        (_, token, book, side, quantity, price, tif, open_close, _, _, _,
         display, _, off_hours, smp_level, smp_method, _) = _ENTER.unpack(payload)
        if side not in (b"B", b"S") or any((tif, open_close, display, off_hours, smp_level, smp_method)):
            raise ValueError("unsupported OUCH Enter attributes")
        return Command("O", _read_token(token), book, side.decode("ascii"), price, quantity)
    if kind == b"U" and len(payload) == _REPLACE.size:
        _, previous, token, quantity, price, open_close, _, _, _, display, category = _REPLACE.unpack(payload)
        if any((open_close, display, category)):
            raise ValueError("unsupported OUCH Replace attributes")
        return Command("U", _read_token(token), price=price, quantity=quantity,
                       previous_token=_read_token(previous))
    if kind == b"X" and len(payload) == _CANCEL.size:
        return Command("X", _read_token(_CANCEL.unpack(payload)[1]))
    raise ValueError("unknown OUCH command type or length")


def encode_reply(reply: Reply) -> bytes:
    head = (reply.kind.encode("ascii"), reply.time_ns, _token(reply.token))
    side = reply.side.encode("ascii")
    if reply.kind == "A":
        payload = _ACCEPTED.pack(*head, reply.book_id, side, reply.order_id, reply.quantity,
                                 reply.price, 0, 0, _CLIENT, 1, _CUSTOMER, _ACCOUNT,
                                 reply.pre_trade_quantity, 0, _CLIENT_CATEGORY, 0, 0, 0, b"   ")
    elif reply.kind == "U":
        payload = _REPLACED.pack(*head, _token(reply.previous_token), reply.book_id, side,
                                 reply.order_id, reply.quantity, reply.price, 0, 0, _CLIENT,
                                 1 if reply.quantity else 2, _CUSTOMER, _ACCOUNT,
                                 reply.pre_trade_quantity, 0, _CLIENT_CATEGORY)
    elif reply.kind == "C":
        payload = _CANCELED.pack(*head, reply.book_id, side, reply.order_id, reply.reason)
    elif reply.kind == "E":
        payload = _EXECUTED.pack(*head, reply.book_id, reply.quantity, reply.price,
                                 reply.match_id.to_bytes(12, "big"), _CLIENT_CATEGORY)
    elif reply.kind == "J":
        payload = _REJECTED.pack(*head, reply.reason)
    else:
        raise ValueError("unknown OUCH reply type")
    return data_frame(payload, b"S")


def decode_reply(frame: bytes) -> Reply:
    payload = _payload(frame, b"S")
    kind = payload[:1]
    if kind == b"A" and len(payload) == _ACCEPTED.size:
        _, time, token, book, side, order, quantity, price = _ACCEPTED.unpack(payload)[:8]
        return Reply("A", time, _read_token(token), book, side.decode("ascii"), order, price, quantity)
    if kind == b"U" and len(payload) == _REPLACED.size:
        _, time, token, previous, book, side, order, quantity, price = _REPLACED.unpack(payload)[:9]
        return Reply("U", time, _read_token(token), book, side.decode("ascii"), order, price,
                     quantity, _read_token(previous))
    if kind == b"C" and len(payload) == _CANCELED.size:
        _, time, token, book, side, order, reason = _CANCELED.unpack(payload)
        return Reply("C", time, _read_token(token), book, side.decode("ascii"), order, reason=reason)
    if kind == b"E" and len(payload) == _EXECUTED.size:
        _, time, token, book, quantity, price, match, _ = _EXECUTED.unpack(payload)
        return Reply("E", time, _read_token(token), book, price=price, quantity=quantity,
                     match_id=int.from_bytes(match, "big"))
    if kind == b"J" and len(payload) == _REJECTED.size:
        _, time, token, reason = _REJECTED.unpack(payload)
        return Reply("J", time, _read_token(token), reason=reason)
    raise ValueError("unknown OUCH reply type or length")
