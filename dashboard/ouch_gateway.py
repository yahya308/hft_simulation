"""Loopback-only SoupBinTCP session for simulated BISTECH OUCH orders.

The owner schedules ExternalCommand events and publishes the resulting replies.
The gateway keeps transport state only. Its journal is bounded and in memory.
"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from hmac import compare_digest
import logging
from select import select
import socket
from threading import Event, Lock, Thread, current_thread
from time import monotonic

from .ouch import (Command, Reply, decode_command, decode_login_request, decode_reply,
                   encode_login_accepted, encode_login_rejected, encode_login_request,
                   encode_reply, soup_packet, split_soup_packet)


_HOST = "127.0.0.1"
_HEARTBEAT = soup_packet(b"H")
_END_SESSION = soup_packet(b"Z")
_MAX_WAIT_SECONDS = 0.25
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExternalCommand:
    sequence: int
    command: Command
    frame: bytes
    generation: int


@dataclass(slots=True)
class _Client:
    last_received: float
    last_sent: float
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytes = b""
    next_reply: int | None = None
    generation: int = 0
    closing: bool = False
    close_deadline: float | None = None


class OuchGateway:
    """One authenticated local client at a time. The submit callback must return promptly."""

    def __init__(self, submit: Callable[[ExternalCommand], None], *, port: int = 0,
                 username: str = "SIM", password: str = "SIMTEST", session: str = "SIM0000001",
                 replay_capacity: int = 4096, heartbeat_seconds: float = 1.0,
                 client_timeout: float = 15.0, login_timeout: float = 5.0) -> None:
        if not 0 <= port <= 65535 or replay_capacity < 1:
            raise ValueError("invalid OUCH gateway port or replay capacity")
        if not 0 < heartbeat_seconds < client_timeout or not 0 < login_timeout:
            raise ValueError("invalid OUCH gateway timeouts")
        encode_login_request(username, password, session=session)
        encode_login_accepted(session, 1)
        self._username, self._password = username.upper(), password.upper()
        self._session = session
        self._port = port
        self._submit = submit
        self._replay_capacity = replay_capacity
        self._heartbeat_seconds = heartbeat_seconds
        self._client_timeout = client_timeout
        self._login_timeout = login_timeout
        self._lock = Lock()
        self._stopping = Event()
        self._thread: Thread | None = None
        self._wakeup: socket.socket | None = None
        self._address: tuple[str, int] | None = None
        self._generation = 1
        self._ended = False
        self._next_intake = 1
        self._next_reply = 1
        self._replies: OrderedDict[int, bytes] = OrderedDict()

    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise RuntimeError("OUCH gateway is not started")
        return self._address

    @property
    def session(self) -> str:
        with self._lock:
            return self._session

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def start(self) -> tuple[str, int]:
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("OUCH gateway is closed")
            if self._thread is not None:
                return self.address
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((_HOST, self._port))
                listener.listen(4)
                listener.setblocking(False)
                wake_reader, wake_writer = self._wake_sockets()
            except BaseException:
                listener.close()
                raise
            self._address = listener.getsockname()
            self._wakeup = wake_writer
            self._thread = Thread(target=self._run, args=(listener, wake_reader, wake_writer),
                                  name="ouch-gateway", daemon=True)
            self._thread.start()
            return self.address

    def __enter__(self) -> "OuchGateway":
        self.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Stop the listener and finish the active socket without waiting for a slow peer."""
        with self._lock:
            self._stopping.set()
            thread = self._thread
        self._notify()
        if thread is not None and thread is not current_thread():
            thread.join(2.0)
            if thread.is_alive():
                raise RuntimeError("OUCH gateway submit callback did not stop")

    def reset_session(self, session: str | None = None) -> int:
        """Rotate the logical session and invalidate prior client intake and replies."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("OUCH gateway is closed")
            generation = self._generation + 1
            new_session = session if session is not None else f"SIM{generation:07d}"
            if new_session == self._session and session is None:
                new_session = f"R{generation:09d}"
            encode_login_accepted(new_session, 1)
            if new_session == self._session:
                raise ValueError("OUCH session reset requires a new session ID")
            self._generation = generation
            self._session = new_session
            self._ended = False
            self._next_reply = 1
            self._replies.clear()
        self._notify()
        return generation

    def end_session(self) -> None:
        """Deliver pending replies and End of Session, then reject logins until reset."""
        with self._lock:
            self._ended = True
        self._notify()

    def publish(self, reply: Reply | bytes, *, generation: int | None = None) -> int | None:
        """Journal one sequenced reply, or discard a reply for an older generation."""
        frame = encode_reply(reply) if isinstance(reply, Reply) else bytes(reply)
        if not isinstance(reply, Reply):
            decode_reply(frame)
        with self._lock:
            if generation is not None and generation != self._generation:
                return None
            if self._stopping.is_set() or self._ended:
                raise RuntimeError("OUCH session is closed")
            sequence = self._next_reply
            self._replies[sequence] = frame
            self._next_reply += 1
            if len(self._replies) > self._replay_capacity:
                self._replies.popitem(last=False)
        self._notify()
        return sequence

    def _notify(self) -> None:
        if self._wakeup is not None:
            try:
                self._wakeup.send(b"\0")
            except OSError:
                pass  # A full wakeup socket is already readable, or the listener is closed.

    @staticmethod
    def _wake_sockets() -> tuple[socket.socket, socket.socket]:
        # UDP needs no blocking connect/accept pair on Windows. Both ends stay on loopback.
        reader = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            writer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                reader.bind((_HOST, 0))
                writer.bind((_HOST, 0))
                writer.connect(reader.getsockname())
                reader.connect(writer.getsockname())
                reader.setblocking(False)
                writer.setblocking(False)
            except BaseException:
                writer.close()
                raise
        except BaseException:
            reader.close()
            raise
        return reader, writer

    def _run(self, listener: socket.socket, wake: socket.socket, writer: socket.socket) -> None:
        with listener, wake, writer:
            while not self._stopping.is_set():
                readable, _, _ = select([listener, wake], [], [], _MAX_WAIT_SECONDS)
                if wake in readable:
                    wake.recv(4096)
                if listener not in readable or self._stopping.is_set():
                    continue
                try:
                    peer, _ = listener.accept()
                    with peer:
                        peer.setblocking(False)
                        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        self._serve(peer, listener, wake)
                except (OSError, ValueError):
                    continue  # Broken socket or malformed client frame.

    def _serve(self, peer: socket.socket, listener: socket.socket, wake: socket.socket) -> None:
        now = monotonic()
        login_deadline = now + self._login_timeout
        client = _Client(now, now)
        while True:
            now = monotonic()
            if not self._prepare_output(client, now):
                return
            if client.close_deadline is not None:
                deadline = client.close_deadline
            elif client.next_reply is None:
                deadline = login_deadline
            else:
                deadline = client.last_received + self._client_timeout
            if now >= deadline:
                return
            if client.next_reply is not None and not client.outgoing:
                deadline = min(deadline, client.last_sent + self._heartbeat_seconds)
            readers = [wake] if client.closing or client.close_deadline is not None else [wake, listener, peer]
            writers = [peer] if client.outgoing else []
            timeout = min(_MAX_WAIT_SECONDS, max(0.0, deadline - now))
            readable, writable, _ = select(readers, writers, [], timeout)
            if wake in readable:
                wake.recv(4096)
            if peer in readable and not self._receive(peer, client):
                return
            if listener in readable and not client.closing:
                extra, _ = listener.accept()
                extra.close()
            if peer in writable:
                sent = peer.send(client.outgoing)
                if not sent:
                    return
                client.outgoing = client.outgoing[sent:]
                client.last_sent = monotonic()

    def _prepare_output(self, client: _Client, now: float) -> bool:
        if client.closing:
            return bool(client.outgoing)
        if client.next_reply is None:
            return not self._stopping.is_set()
        with self._lock:
            current = client.generation == self._generation and not self._stopping.is_set()
            frame = self._replies.get(client.next_reply) if current else None
            fell_behind = current and frame is None and client.next_reply < self._next_reply
            ended = not current or (self._ended and frame is None)
        if fell_behind:
            return False  # The client must reconnect at a retained sequence or restart its test.
        if ended and client.close_deadline is None:
            client.close_deadline = now + 0.5
        if client.outgoing:
            return True
        if ended:
            client.outgoing = _END_SESSION
            client.closing = True
        elif frame is not None:
            client.outgoing = frame
            client.next_reply += 1
        elif now - client.last_sent >= self._heartbeat_seconds:
            client.outgoing = _HEARTBEAT
        return True

    def _receive(self, peer: socket.socket, client: _Client) -> bool:
        data = peer.recv(16_384)
        if not data:
            return False
        client.incoming.extend(data)
        offset = 0
        while len(client.incoming) - offset >= 2:
            length = int.from_bytes(client.incoming[offset:offset + 2], "big")
            if length == 0:
                raise ValueError("invalid SoupBinTCP packet length")
            end = offset + length + 2
            if len(client.incoming) < end:
                break
            frame = bytes(client.incoming[offset:end])
            offset = end
            if not self._handle_frame(client, frame):
                return False
            client.last_received = monotonic()
            if client.closing:
                break
        del client.incoming[:offset]
        return True

    def _handle_frame(self, client: _Client, frame: bytes) -> bool:
        kind, payload = split_soup_packet(frame)
        if kind == b"+":
            return True
        if client.next_reply is None:
            self._login(client, frame)
            return True
        if kind == b"R" and not payload:
            return True
        if kind == b"O" and not payload:
            return False
        command = decode_command(frame)
        with self._lock:
            if client.generation != self._generation or self._ended or self._stopping.is_set():
                return True
            request = ExternalCommand(self._next_intake, command, frame, client.generation)
            self._next_intake += 1
        try:
            self._submit(request)
        except Exception:
            _LOGGER.exception("OUCH sandbox submit callback failed")
            return False
        return True

    def _login(self, client: _Client, frame: bytes) -> None:
        try:
            request = decode_login_request(frame)
        except ValueError:
            self._reject_login(client, b"A")
            return
        user_matches = compare_digest(request.username.upper(), self._username)
        password_matches = compare_digest(request.password.upper(), self._password)
        if not (user_matches and password_matches):
            self._reject_login(client, b"A")
            return
        with self._lock:
            sequence = request.sequence or max(1, self._next_reply - 1)
            oldest = self._next_reply - len(self._replies)
            if (self._ended or request.session not in ("", self._session) or
                    not oldest <= sequence <= self._next_reply):
                self._reject_login(client, b"S")
                return
            client.next_reply = sequence
            client.generation = self._generation
            client.outgoing = encode_login_accepted(self._session, sequence)

    @staticmethod
    def _reject_login(client: _Client, reason: bytes) -> None:
        client.outgoing = encode_login_rejected(reason)
        client.closing = True
        client.close_deadline = monotonic() + 0.5
