import argparse
import hashlib
import random
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from replay_test import (DESTINATION, PORT, SECOND, add, delete, directory, execute,
                         mold, state, system_event, tick_size, trade, udp_frame)


MESSAGES_PER_SECOND = 100_000
MAX_PACKET_MESSAGES = 24
MAX_MOLD_BYTES = 1472  # 1500-byte IPv4 MTU, less IP and UDP headers.
PACKET_TIMING_KEYS = ("packet_p50_ns", "packet_p95_ns", "packet_p99_ns",
                      "packet_p999_ns", "packet_max_ns")
PACKET_TIMING_LABELS = ("p50", "p95", "p99", "p99.9", "max")
METRIC_KEYS = ("replay_ns", "snapshot_ns", "selected_packets", "messages",
               "data_packets", "samples", *PACKET_TIMING_KEYS)


@dataclass
class Fixture:
    file_bytes: int
    messages: int
    packets: int
    live_orders: int


class CaptureWriter:
    def __init__(self, output):
        self.output = output
        self.output.write(struct.pack("<IHHIIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))
        self.messages = 0
        self.packets = 0
        self.events = 0
        self.last_ns = 0
        self.batch = []
        self.batch_bytes = 20

    def add_event(self, item):
        second_index, offset = divmod(self.events, MESSAGES_PER_SECOND)
        second = SECOND + second_index
        nanosecond = offset * (1_000_000_000 // MESSAGES_PER_SECOND)
        timestamp = second * 1_000_000_000 + nanosecond
        if offset == 0:
            self.append(struct.pack("!cI", b"T", second), timestamp)
        struct.pack_into("!I", item, 1, nanosecond)
        self.append(item, timestamp)
        self.events += 1

    def append(self, item, timestamp):
        block_size = 2 + len(item)
        if self.batch_bytes + block_size > MAX_MOLD_BYTES or len(self.batch) == MAX_PACKET_MESSAGES:
            self.flush()
        self.batch.append(item)
        self.batch_bytes += block_size
        self.messages += 1
        self.last_ns = timestamp

    def flush(self):
        if self.batch:
            self.write_packet(mold(self.messages - len(self.batch) + 1, self.batch), self.last_ns)
            self.batch.clear()
            self.batch_bytes = 20

    def close_session(self):
        self.add_event(system_event(b"C"))
        self.flush()
        self.write_packet(mold(self.messages + 1, count=0xFFFF), self.last_ns)

    def write_packet(self, payload, timestamp):
        if len(payload) > MAX_MOLD_BYTES:
            raise RuntimeError("synthetic UDP datagram exceeds the MTU")
        frame = udp_frame(payload)
        seconds, nanoseconds = divmod(timestamp, 1_000_000_000)
        self.output.write(struct.pack("<IIII", seconds, nanoseconds, len(frame), len(frame)))
        self.output.write(frame)
        self.packets += 1


def write_fixture(path, books, orders_per_side, messages, seed):
    rng = random.Random(seed)
    slots_per_book = 2 * orders_per_side
    live_orders = books * slots_per_book
    order_ids = list(range(1, live_orders + 1))
    quantities = [0] * live_orders
    next_order = live_orders + 1

    with path.open("wb") as output:
        capture = CaptureWriter(output)
        capture.add_event(system_event(b"O"))
        for book in range(1, books + 1):
            capture.add_event(directory(book, f"SYMBOL{book:06}".encode("ascii")))
            capture.add_event(tick_size(book))
            capture.add_event(state(book))

        def add_slot(slot):
            book, side_index = divmod(slot, slots_per_book)
            side = b"B" if side_index < orders_per_side else b"S"
            offset = 50 * rng.randrange(64)
            price = 10_000 - offset if side == b"B" else 10_100 + offset
            quantities[slot] = rng.randint(10, 100)
            rank_time = SECOND * 1_000_000_000 + 10_000 * capture.events
            capture.add_event(add(book + 1, side, order_ids[slot], price, rank_time, 1, quantities[slot]))

        for slot in range(live_orders):
            add_slot(slot)

        remaining = messages
        while remaining:
            slot = rng.randrange(live_orders)
            book_index, side_index = divmod(slot, slots_per_book)
            book = book_index + 1
            side = b"B" if side_index < orders_per_side else b"S"
            order = order_ids[slot]
            quantity = quantities[slot]
            action = rng.randrange(4)
            if (action == 0 or remaining == 1) and quantity > 1:
                fill = rng.randint(1, min(quantity - 1, 10))
                quantities[slot] -= fill
                trade_price = 10_000 if rng.randrange(2) else None
                capture.add_event(execute(book, side, order, fill, trade_price))
                remaining -= 1
                continue
            if remaining == 1:
                capture.add_event(trade(book))
                break

            if action <= 1:
                trade_price = 10_000 if rng.randrange(2) else None
                capture.add_event(execute(book, side, order, quantity, trade_price))
            else:
                capture.add_event(delete(book, side, order))
            if action != 3:  # Replace uses the existing ID after the delete.
                order_ids[slot] = next_order
                next_order += 1
            add_slot(slot)
            remaining -= 2

        capture.close_session()

    return Fixture(path.stat().st_size, capture.messages, capture.packets, live_orders)


def run_replay(command, *, keep_snapshot=False):
    started = time.perf_counter_ns()
    result = subprocess.run(command, stdout=subprocess.PIPE if keep_snapshot else subprocess.DEVNULL,
                            stderr=subprocess.PIPE, check=False)
    elapsed = time.perf_counter_ns() - started
    if result.returncode != 0:
        raise RuntimeError(f"hft_replay exited with {result.returncode}: "
                           f"{result.stderr.decode('utf-8', errors='replace').strip()}")
    return elapsed, result


def read_metrics(stderr, fixture):
    lines = stderr.decode("utf-8", errors="replace").splitlines()
    if len(lines) != 1 or not lines[0].startswith("METRICS\t"):
        raise RuntimeError(f"expected one METRICS line on stderr, received {lines!r}")
    fields = lines[0].split("\t")[1:]
    try:
        values = dict(field.split("=", 1) for field in fields)
        metrics = {key: int(values[key]) for key in METRIC_KEYS}
    except (KeyError, ValueError) as error:
        raise RuntimeError(f"invalid METRICS line: {lines[0]}") from error
    if len(values) != len(fields) or any(value < 0 for value in metrics.values()):
        raise RuntimeError(f"invalid METRICS line: {lines[0]}")
    if (metrics["messages"], metrics["selected_packets"], metrics["data_packets"]) != (
            fixture.messages, fixture.packets, fixture.packets - 1):
        raise RuntimeError(f"replay metrics do not match fixture counts: {lines[0]}")
    return metrics


def verify_snapshot(command, metrics_command, fixture):
    _, baseline = run_replay(command, keep_snapshot=True)
    _, repeated = run_replay(command, keep_snapshot=True)
    _, measured = run_replay(metrics_command, keep_snapshot=True)
    read_metrics(measured.stderr, fixture)
    if baseline.stdout != repeated.stdout or baseline.stdout != measured.stdout:
        raise RuntimeError("replay snapshots differ between runs or metric modes")

    header = baseline.stdout.split(b"\n", 1)[0].rstrip(b"\r").split(b"\t")
    if (len(header) != 5 or header[0] != b"REPLAY" or header[4] != b"mold_end" or
            header[1] != str(fixture.messages).encode("ascii") or
            baseline.stdout.count(b"\nORDER\t") != fixture.live_orders):
        raise RuntimeError("replay snapshot does not match fixture sequence, end state or live order count")
    print(f"Snapshot SHA-256 ({fixture.live_orders:,} orders, identical in three runs): "
          f"{hashlib.sha256(baseline.stdout).hexdigest()}", flush=True)


def describe_range(label, values, unit, *, precision=2):
    median, low, high = statistics.median(values), min(values), max(values)
    print(f"  {label}: median {median:,.{precision}f} {unit}, "
          f"range {low:,.{precision}f} to {high:,.{precision}f} {unit}")


def measure(command, metrics_command, fixture, runs):
    print("Warming up both modes outside measurement.", flush=True)
    run_replay(command)
    _, warm_metrics = run_replay(metrics_command)
    read_metrics(warm_metrics.stderr, fixture)
    baseline_times = []
    instrumented_times = []
    metrics_runs = []
    for index in range(runs):
        elapsed, _ = run_replay(command)
        baseline_times.append(elapsed)
        seconds = elapsed / 1_000_000_000
        print(f"Baseline {index + 1}/{runs}: {elapsed / 1e6:,.2f} ms, "
              f"{fixture.messages / seconds:,.0f} messages/s", flush=True)
    for index in range(runs):
        elapsed, result = run_replay(metrics_command)
        instrumented_times.append(elapsed)
        metrics = read_metrics(result.stderr, fixture)
        metrics_runs.append(metrics)
        packet_times = ", ".join(
            f"{label}={metrics[key] / 1e3:.2f}" for label, key in zip(PACKET_TIMING_LABELS, PACKET_TIMING_KEYS)
        )
        print(f"Metrics {index + 1}/{runs}: wall {elapsed / 1e6:,.2f} ms, "
              f"replay {metrics['replay_ns'] / 1e6:,.2f} ms, snapshot {metrics['snapshot_ns'] / 1e6:,.2f} ms, "
              f"{metrics['samples']:,} samples ({packet_times} us)", flush=True)

    print("Baseline wall-clock summary")
    describe_range("elapsed", [value / 1e6 for value in baseline_times], "ms")
    describe_range("throughput", [fixture.messages * 1e9 / value for value in baseline_times],
                   "ITCH messages/s", precision=0)
    describe_range("packet rate", [fixture.packets * 1e9 / value for value in baseline_times],
                   "PCAP packets/s", precision=0)
    describe_range("file rate", [fixture.file_bytes * 1e9 / (2**20 * value) for value in baseline_times], "MiB/s")
    print("Instrumented summary")
    describe_range("wall", [value / 1e6 for value in instrumented_times], "ms")
    for key in ("replay_ns", "snapshot_ns"):
        describe_range(key.removesuffix("_ns"), [metrics[key] / 1e6 for metrics in metrics_runs], "ms")
    for label, key in zip(PACKET_TIMING_LABELS, PACKET_TIMING_KEYS):
        describe_range(f"packet {label}", [metrics[key] / 1e3 for metrics in metrics_runs], "us")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description="Benchmark deterministic synthetic BISTECH ITCH PCAP replay.")
    parser.add_argument("executable", type=Path, help="path to a Release build of hft_replay")
    parser.add_argument("--books", type=positive, default=32, help="order books (default: 32)")
    parser.add_argument("--orders-per-side", type=positive, default=256,
                        help="live slots per side and book (default: 256)")
    parser.add_argument("--messages", type=positive, default=1_000_000,
                        help="workload messages after seeding, before session close (default: 1000000)")
    parser.add_argument("--runs", type=positive, default=5, help="measured runs per mode (default: 5)")
    parser.add_argument("--seed", type=int, default=2112, help="workload random seed (default: 2112)")
    args = parser.parse_args()
    if not args.executable.is_file():
        parser.error(f"executable not found: {args.executable}")

    try:
        with tempfile.TemporaryDirectory(prefix="hft-benchmark-") as temp:
            path = Path(temp) / "benchmark.pcap"
            fixture = write_fixture(path, args.books, args.orders_per_side, args.messages, args.seed)
            print(f"Fixture: seed {args.seed}, {args.books:,} books, {args.orders_per_side:,} live slots/side, "
                  f"{args.messages:,} workload / {fixture.messages:,} total ITCH messages, "
                  f"{fixture.packets:,} PCAP packets, {fixture.file_bytes:,} bytes", flush=True)
            address = ".".join(str(octet) for octet in DESTINATION)
            command = [str(args.executable.resolve()), str(path), address, str(PORT)]
            metrics_command = [*command, "--metrics"]
            verify_snapshot(command, metrics_command, fixture)
            measure(command, metrics_command, fixture, args.runs)
    except (OSError, RuntimeError, struct.error) as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
