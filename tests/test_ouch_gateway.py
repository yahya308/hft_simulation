import socket
import unittest

from dashboard.ouch import (Command, Reply, decode_login_accepted, decode_reply,
                            encode_command, encode_login_request, soup_packet, split_soup_packet)
from dashboard.ouch_gateway import OuchGateway


def read_exact(peer, length):
    data = bytearray()
    while len(data) < length:
        chunk = peer.recv(length - len(data))
        if not chunk:
            raise EOFError("incomplete SoupBinTCP packet")
        data.extend(chunk)
    return bytes(data)


def read_frame(peer):
    header = read_exact(peer, 2)
    return header + read_exact(peer, int.from_bytes(header, "big"))


def log_in(peer, *, session="", sequence=1, password="simtest"):
    peer.sendall(encode_login_request("sim", password, session=session, sequence=sequence))
    return read_frame(peer)


class OuchGatewayTest(unittest.TestCase):
    def test_fragmented_login_coalesced_commands_and_resume(self):
        commands = []

        def submit(request):
            commands.append(request)
            gateway.publish(Reply("J", 100 + request.sequence, request.command.token, reason=-1),
                            generation=request.generation)

        requests = [Command("O", "ONE", 42, "B", 1800, 300),
                    Command("U", "TWO", price=1850, quantity=200, previous_token="ONE"),
                    Command("X", "ONE")]
        frames = [encode_command(command) for command in requests]
        # Construct the login fixture independently from the codec helper.
        login = b"\x00/L" + b"sim   simtest   " + b" " * 10 + b" " * 19 + b"1"
        with OuchGateway(submit) as gateway:
            self.assertEqual(gateway.address[0], "127.0.0.1")
            with socket.create_connection(gateway.address, timeout=2) as peer:
                peer.sendall(login[:1])
                peer.sendall(login[1:21])
                peer.sendall(login[21:] + b"".join(frames))
                self.assertEqual(decode_login_accepted(read_frame(peer)), (gateway.session, 1))
                replies = [decode_reply(read_frame(peer)) for _ in requests]
                self.assertEqual([reply.time_ns for reply in replies], [101, 102, 103])
                self.assertEqual([reply.token for reply in replies], ["ONE", "TWO", "ONE"])
                self.assertEqual([request.command for request in commands], requests)
                self.assertEqual([request.frame for request in commands], frames)
                self.assertEqual([request.sequence for request in commands], [1, 2, 3])
                peer.sendall(soup_packet(b"O"))
                self.assertEqual(peer.recv(1), b"")

            with socket.create_connection(gateway.address, timeout=2) as peer:
                self.assertEqual(decode_login_accepted(log_in(peer, session=gateway.session, sequence=2)),
                                 (gateway.session, 2))
                self.assertEqual(decode_reply(read_frame(peer)), replies[1])
                self.assertEqual(decode_reply(read_frame(peer)), replies[2])

    def test_authorization_replay_bounds_and_reset(self):
        with OuchGateway(lambda request: self.fail("unexpected command"), replay_capacity=2) as gateway:
            initial_session = gateway.session
            for sequence in range(1, 4):
                self.assertEqual(gateway.publish(Reply("J", sequence, f"R{sequence}", reason=-1)), sequence)

            cases = (({"password": "wrong"}, b"A"), ({"sequence": 1}, b"S"),
                     ({"sequence": 5}, b"S"), ({"session": "other", "sequence": 2}, b"S"))
            for options, reason in cases:
                with self.subTest(options=options), socket.create_connection(gateway.address, timeout=2) as peer:
                    self.assertEqual(split_soup_packet(log_in(peer, **options)), (b"J", reason))
                    self.assertEqual(peer.recv(1), b"")

            with socket.create_connection(gateway.address, timeout=2) as peer:
                self.assertEqual(decode_login_accepted(log_in(peer, sequence=0)), (initial_session, 3))
                self.assertEqual(decode_reply(read_frame(peer)).token, "R3")
                self.assertEqual(gateway.reset_session(), 2)
                self.assertEqual(gateway.publish(Reply("J", 4, "OLD", reason=-1), generation=1), None)
                self.assertEqual(split_soup_packet(read_frame(peer)), (b"Z", b""))
                self.assertEqual(peer.recv(1), b"")

            with socket.create_connection(gateway.address, timeout=2) as peer:
                self.assertEqual(split_soup_packet(log_in(peer, session=initial_session)), (b"J", b"S"))
                self.assertEqual(peer.recv(1), b"")
            with socket.create_connection(gateway.address, timeout=2) as peer:
                self.assertEqual(decode_login_accepted(log_in(peer)), (gateway.session, 1))
                self.assertEqual(gateway.publish(Reply("J", 4, "NEW", reason=-1), generation=2), 1)
                gateway.end_session()
                self.assertEqual(decode_reply(read_frame(peer)).token, "NEW")
                self.assertEqual(split_soup_packet(read_frame(peer)), (b"Z", b""))
                self.assertEqual(peer.recv(1), b"")

    def test_client_heartbeat_and_shutdown(self):
        with OuchGateway(lambda request: self.fail("unexpected command"), heartbeat_seconds=0.05) as gateway:
            with socket.create_connection(gateway.address, timeout=2) as peer:
                self.assertEqual(decode_login_accepted(log_in(peer)), (gateway.session, 1))
                peer.sendall(soup_packet(b"R"))
                self.assertEqual(split_soup_packet(read_frame(peer)), (b"H", b""))
                gateway.close()
                # A heartbeat already in the socket can precede End of Session.
                packet = split_soup_packet(read_frame(peer))
                while packet == (b"H", b""):
                    packet = split_soup_packet(read_frame(peer))
                self.assertEqual(packet, (b"Z", b""))
                self.assertEqual(peer.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
