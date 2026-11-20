import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


DESTINATION = bytes((239, 1, 2, 3))
SOURCE = bytes((10, 0, 0, 1))
PORT = 31000
SESSION = b"TESTDAY   "
SECOND = 1_700_000_000


def message(kind, length, nanoseconds=100):
    data = bytearray(length)
    struct.pack_into("!cI", data, 0, kind, nanoseconds)
    return data


def seconds():
    return struct.pack("!cI", b"T", SECOND)


def system_event(code):
    return message(b"S", 5) + code


def directory(book, symbol, *, financial_product=5):
    data = message(b"R", 130)
    struct.pack_into("!I32s", data, 5, book, symbol.ljust(32))
    data[85] = financial_product
    struct.pack_into("!H", data, 89, 4)
    struct.pack_into("!I", data, 97, 1)
    data[129] = 1
    return data


def tick_size(book):
    data = message(b"L", 25)
    struct.pack_into("!IQii", data, 5, book, 50, 0, 0)
    return data


def state(book):
    data = message(b"O", 29)
    struct.pack_into("!I20s", data, 5, book, b"CONTINUOUS".ljust(20))
    return data


def add(book, side, order, price, rank_time, rank_sequence, quantity=10):
    data = message(b"A", 45)
    struct.pack_into("!QIcIQiHBQ", data, 5, order, book, side, rank_sequence,
                     quantity, price, 0, 2, rank_time)
    return data


def execute(book, side, order, quantity, price=None, *, match_id=123, at_cross=b"N", printable=b"N"):
    data = message(b"E" if price is None else b"C", 52 if price is None else 58)
    struct.pack_into("!QIcQQ", data, 5, order, book, side, quantity, match_id)
    if price is not None:
        struct.pack_into("!icc", data, 52, price, at_cross, printable)
    return data


def delete(book, side, order):
    data = message(b"D", 18)
    struct.pack_into("!QIc", data, 5, order, book, side)
    return data


def flush(book):
    return message(b"Y", 5) + struct.pack("!I", book)


def trade(book):
    data = message(b"P", 50)
    struct.pack_into("!QIcQIi", data, 5, 123, 0, b" ", 70, book, 1000)
    data[48:50] = b"YN"
    return data


def mold(sequence, messages=(), *, session=SESSION, count=None):
    if count is None:
        count = len(messages)
    return struct.pack("!10sQH", session, sequence, count) + b"".join(
        struct.pack("!H", len(item)) + item for item in messages
    )


def udp_frame(payload, *, port=PORT, source=SOURCE, vlan=False, udp_length=None):
    if udp_length is None:
        udp_length = len(payload) + 8
    udp = struct.pack("!HHHH", 32000, port, udp_length, 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, 1, 0, 1, 17, 0,
                     source, DESTINATION) + udp
    protocol = b"\x88\xa8\x00\x01\x81\x00\x00\x02\x08\x00" if vlan else b"\x08\x00"
    return bytes(12) + protocol + ip


def pcap(frames, *, endian="<", nano=False):
    magic = 0xA1B23C4D if nano else 0xA1B2C3D4
    data = bytearray(struct.pack(endian + "IHHIIII", magic, 2, 4, 0, 0, 65535, 1))
    for index, frame in enumerate(frames):
        # Capture timestamps deliberately decrease. They do not drive ITCH sequencing.
        fraction = 500 - index
        data += struct.pack(endian + "IIII", SECOND, fraction, len(frame), len(frame))
        data += frame
    return bytes(data)


class ReplayTest(unittest.TestCase):
    def replay(self, capture, *options):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.pcap"
            path.write_bytes(capture)
            return subprocess.run([EXECUTABLE, path, "239.1.2.3", str(PORT), *options],
                                  check=False, capture_output=True, encoding="utf-8")

    def assert_failed(self, frames, reason):
        result = self.replay(pcap(frames))
        self.assertEqual(result.returncode, 1, result)
        self.assertEqual(result.stdout, "")
        self.assertIn(reason, result.stderr)

    def test_snapshot_and_transport_order(self):
        intro = [seconds(), system_event(b"O"), directory(1, b"ONE"), directory(2, b"TWO"),
                 tick_size(1), state(1), add(1, b"B", 8, 1000, 300, 1),
                 add(1, b"B", 7, 1000, 200, 3), add(1, b"B", 9, 1000, 200, 1),
                 add(1, b"S", 7, 1200, 100, 1), add(2, b"B", 7, 900, 100, 1)]
        updates = [execute(1, b"B", 7, 3), execute(1, b"B", 7, 2, 1100),
                   delete(1, b"B", 8), add(1, b"B", 8, 1050, 350, 1), trade(1),
                   add(1, b"S", 10, 1250, 100, 2, quantity=0),
                   add(1, b"S", 11, -(2**31), 100, 3),
                   add(1, b"S", 12, 1100, 100, 4),
                   execute(1, b"S", 12, 10),
                   flush(2), directory(1, b"ONE_NEW")]
        overlapping = mold(len(intro), [intro[-1], *updates])
        last_sequence = len(intro) + len(updates)
        frames = [udp_frame(mold(33), port=1), udp_frame(mold(1, intro), vlan=True),
                  udp_frame(mold(1, intro)), udp_frame(overlapping),
                  udp_frame(mold(last_sequence + 1))]
        snapshot = [
            "BOOK\t1\tONE_NEW\t4\t1\t1\tCONTINUOUS",
            "TICK\t1\t0\t0\t50",
            "ORDER\t1\tB\t1050\t350\t1\t8\t10",
            "ORDER\t1\tB\t1000\t200\t1\t9\t10",
            "ORDER\t1\tB\t1000\t200\t3\t7\t5",
            "ORDER\t1\tS\t1200\t100\t1\t7\t10",
            "BOOK\t2\tTWO\t4\t1\t1\t",
        ]
        for endian in ("<", ">"):
            for nano in (False, True):
                with self.subTest(endian=endian, nano=nano):
                    capture = pcap(frames, endian=endian, nano=nano)
                    result = self.replay(capture)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    capture_time = SECOND * 10**9 + 497 * (1 if nano else 1000)
                    self.assertEqual(result.stdout.splitlines(), [
                        f"REPLAY\t{last_sequence}\t{capture_time}\t{SECOND * 10**9 + 100}\tcapture_prefix",
                        *snapshot,
                    ])
                    self.assertEqual(result.stdout, self.replay(capture).stdout)

    def test_heartbeat_and_end_of_session(self):
        frames = [udp_frame(mold(1)), udp_frame(mold(1, [seconds(), system_event(b"O")])),
                  udp_frame(mold(3)), udp_frame(mold(3, [system_event(b"C")])),
                  udp_frame(mold(4, count=0xFFFF)), udp_frame(mold(4, count=0xFFFF))]
        result = self.replay(pcap(frames))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("\tmold_end\n"), result.stdout)

    def test_metrics_count_only_new_messages_and_preserve_snapshot(self):
        start = udp_frame(mold(1, [seconds(), system_event(b"O")]))
        capture = pcap([start, start, udp_frame(mold(3)), udp_frame(mold(3, [system_event(b"C")]))])
        normal = self.replay(capture)
        measured = self.replay(capture, "--metrics")
        self.assertEqual(measured.returncode, 0, measured.stderr)
        self.assertEqual(measured.stdout, normal.stdout)
        self.assertEqual(normal.stderr, "")
        kind, *fields = measured.stderr.strip().split("\t")
        self.assertEqual(kind, "METRICS")
        metrics = {key: int(value) for key, value in (field.split("=", 1) for field in fields)}
        self.assertEqual(metrics["messages"], 3)
        self.assertEqual(metrics["selected_packets"], 4)
        self.assertEqual(metrics["data_packets"], 2)
        self.assertEqual(metrics["samples"], 2)
        self.assertLessEqual(metrics["packet_p50_ns"], metrics["packet_p99_ns"])
        self.assertEqual(metrics["packet_p999_ns"], metrics["packet_max_ns"])

    def test_stops_at_the_requested_message_inside_a_packet(self):
        messages = [seconds(), system_event(b"O"), directory(1, b"ONE"), add(1, b"B", 5, 1000, 20, 1),
                    delete(1, b"B", 5)]
        capture = pcap([udp_frame(mold(1, messages))])
        result = self.replay(capture, "--through-sequence", "4", "--metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0].split("\t")[-1], "sequence_limit")
        self.assertIn("ORDER\t1\tB\t1000\t20\t1\t5\t10", result.stdout)
        self.assertIn("\tmessages=4\t", result.stderr)
        self.assertNotIn("ORDER\t", self.replay(capture).stdout)

        for sequence, error in (("0", "precedes"), ("6", "ended before")):
            with self.subTest(sequence=sequence):
                invalid = self.replay(capture, "--through-sequence", sequence)
                self.assertEqual(invalid.returncode, 1)
                self.assertEqual(invalid.stdout, "")
                self.assertIn(error, invalid.stderr)

    def test_gap_stops_before_snapshot(self):
        start = udp_frame(mold(1, [seconds(), system_event(b"O")]))
        for next_message in (mold(4), mold(4, [directory(1, b"ONE")]), mold(4, count=0xFFFF)):
            with self.subTest(count=next_message[18:20]):
                self.assert_failed([start, udp_frame(next_message)], "packet 2: MoldUDP64 sequence gap: expected 3, received 4")

    def test_rejects_missing_start(self):
        self.assert_failed([udp_frame(mold(1, [seconds(), directory(1, b"ONE")]))],
                           "sequence 2: missing ITCH Start of Messages")

    def test_uses_start_event_and_preserves_latin1_text(self):
        start = [seconds(), system_event(b"O"), directory(1, b"\xc7ONE")]
        result = self.replay(pcap([udp_frame(mold(100, start))]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("REPLAY\t102\t"), result.stdout)
        self.assertIn("BOOK\t1\tÇONE\t", result.stdout)

        start[-1] = directory(1, b"ONE\tBAD")
        self.assert_failed([udp_frame(mold(100, start))], "Invalid control byte in ITCH Alpha field")

    def test_rejects_unexpected_sender(self):
        start = udp_frame(mold(1, [seconds(), system_event(b"O")]))
        self.assert_failed([start, udp_frame(mold(3), source=bytes((10, 0, 0, 2)))],
                           "unexpected MoldUDP64 session or UDP sender")

    def test_rejects_invalid_datagrams(self):
        cases = [(udp_frame(mold(1, [seconds()], count=2)), "MoldUDP64 message length"),
                 (udp_frame(mold(1, [b""])), "invalid ITCH block length"),
                 (udp_frame(mold(1) + b"x"), "unexpected payload on MoldUDP64 control packet"),
                 (udp_frame(mold(1, [seconds()]), udp_length=65535), "UDP")]
        for frame, reason in cases:
            with self.subTest(reason=reason):
                self.assert_failed([frame], reason)

    def test_rejects_malformed_itch_with_sequence_context(self):
        items = [seconds(), system_event(b"O"), directory(1, b"ONE"),
                 add(1, b"B", 7, 1000, 1, 1)[:-1]]
        self.assert_failed([udp_frame(mold(1, items))], "packet 1: sequence 4:")

    def test_rejects_invalid_book_mutations(self):
        intro = [seconds(), system_event(b"O"), directory(1, b"ONE"),
                 add(1, b"B", 7, 1000, 1, 1)]
        for item in (execute(1, b"B", 7, 11), delete(1, b"S", 7), intro[-1]):
            with self.subTest(type=item[0]):
                self.assert_failed([udp_frame(mold(1, [*intro, item]))], "packet 1: sequence 5:")


if __name__ == "__main__":
    EXECUTABLE = Path(sys.argv.pop(1)).resolve()
    unittest.main()
