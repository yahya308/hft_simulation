#include "hft/itch_stream.hpp"
#include "hft/order_book.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

std::int64_t elapsed_ns(Clock::time_point start, Clock::time_point end) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
}

class ReplayMetrics {
public:
    ReplayMetrics() { samples_.reserve(sample_limit); }

    void record(std::int64_t packet_ns, std::uint64_t messages) {
        ++selected_packets_;
        messages_ += messages;
        if (messages == 0) {
            return;
        }

        ++data_packets_;
        longest_packet_ns_ = std::max(longest_packet_ns_, packet_ns);
        if (samples_.size() < sample_limit) {
            samples_.push_back(packet_ns);
            return;
        }

        // Reservoir sampling keeps memory bounded over long captures.
        const auto slot = std::uniform_int_distribution<std::uint64_t>(0, data_packets_ - 1)(random_);
        if (slot < sample_limit) {
            samples_[static_cast<std::size_t>(slot)] = packet_ns;
        }
    }

    void write(std::ostream& output, std::int64_t replay_ns, std::int64_t snapshot_ns) {
        std::sort(samples_.begin(), samples_.end());
        output << "METRICS\treplay_ns=" << replay_ns
               << "\tsnapshot_ns=" << snapshot_ns
               << "\tselected_packets=" << selected_packets_
               << "\tmessages=" << messages_
               << "\tdata_packets=" << data_packets_
               << "\tsamples=" << samples_.size()
               << "\tpacket_p50_ns=" << percentile(500)
               << "\tpacket_p95_ns=" << percentile(950)
               << "\tpacket_p99_ns=" << percentile(990)
               << "\tpacket_p999_ns=" << percentile(999)
               << "\tpacket_max_ns=" << longest_packet_ns_ << '\n';
    }

private:
    std::int64_t percentile(std::size_t permille) const {
        if (samples_.empty()) {
            return 0;
        }
        const auto rank = (samples_.size() * permille + 999) / 1000;
        return samples_[rank - 1];
    }

    static constexpr std::size_t sample_limit = 65'536;
    std::minstd_rand random_{1};
    std::vector<std::int64_t> samples_;
    std::uint64_t selected_packets_ = 0;
    std::uint64_t data_packets_ = 0;
    std::uint64_t messages_ = 0;
    std::int64_t longest_packet_ns_ = 0;
};

struct ReplayOptions {
    bool metrics = false;
    std::optional<std::uint64_t> through_sequence;
};

ReplayOptions parse_options(int argc, char** argv) {
    ReplayOptions options;
    for (int index = 4; index < argc; ++index) {
        const std::string_view option(argv[index]);
        if (option == "--metrics" && !options.metrics) {
            options.metrics = true;
        } else if (option == "--through-sequence" && !options.through_sequence && index + 1 < argc) {
            options.through_sequence = hft::parse_sequence_limit(argv[++index]);
        } else {
            throw std::runtime_error("invalid or repeated option: " + std::string(option));
        }
    }
    return options;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: hft_replay <capture.pcap> <destination-ipv4> <udp-port>"
                     " [--metrics] [--through-sequence <sequence>]\n";
        return 2;
    }

    try {
        const auto options = parse_options(argc, argv);
        const auto endpoint = hft::parse_feed_endpoint(argv[2], argv[3]);
        std::ifstream input(argv[1], std::ios::binary);
        if (!input) {
            throw std::runtime_error("could not open capture file");
        }
        hft::PcapReader reader(input);
        hft::ItchStream replay;
        hft::HistoricalBook book;
        hft::UdpPacket packet{};
        std::optional<ReplayMetrics> metrics;
        if (options.metrics) {
            metrics.emplace();
        }
        bool stopped_at_sequence = false;
        const auto replay_start = metrics ? Clock::now() : Clock::time_point{};
        while (reader.next(endpoint, packet)) {
            try {
                const auto packet_start = metrics ? Clock::now() : Clock::time_point{};
                const auto messages = replay.read_packet(packet, options.through_sequence,
                    [&book](const hft::ItchMessage& message, std::uint64_t, std::uint64_t) {
                        book.apply(message);
                    });
                if (metrics) {
                    metrics->record(elapsed_ns(packet_start, Clock::now()), messages);
                }
            } catch (const std::runtime_error& error) {
                throw std::runtime_error("packet " + std::to_string(packet.index) + ": " + error.what());
            }
            if (options.through_sequence && replay.has_reached(*options.through_sequence)) {
                stopped_at_sequence = true;
                break;
            }
        }
        if (options.through_sequence && !stopped_at_sequence) {
            throw std::runtime_error("capture ended before the requested sequence");
        }
        const auto replay_end = metrics ? Clock::now() : Clock::time_point{};
        replay.require_started();
        std::cout << "REPLAY\t" << replay.last_sequence() << '\t' << replay.last_capture_ns()
                  << '\t' << replay.exchange_ns() << '\t' << replay.status(stopped_at_sequence) << '\n';
        book.write_snapshot(std::cout);
        if (!std::cout.flush()) {
            throw std::runtime_error("could not write snapshot");
        }
        if (metrics) {
            metrics->write(std::cerr, elapsed_ns(replay_start, replay_end), elapsed_ns(replay_end, Clock::now()));
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Replay failed: " << error.what() << '\n';
        return 1;
    }
}
