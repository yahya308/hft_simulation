#include "hft/itch.hpp"
#include "hft/moldudp.hpp"
#include "hft/order_book.hpp"
#include "hft/pcap.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
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

template <typename Number>
Number parse_number(std::string_view text, Number maximum, const char* name) {
    Number value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size() || value > maximum) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
    return value;
}

hft::FeedEndpoint parse_endpoint(std::string_view address, std::string_view port) {
    hft::FeedEndpoint endpoint{};
    for (std::size_t i = 0; i < endpoint.destination.size(); ++i) {
        const auto dot = address.find('.');
        const bool last = i + 1 == endpoint.destination.size();
        if (last != (dot == std::string_view::npos)) {
            throw std::runtime_error("invalid destination IPv4 address");
        }
        endpoint.destination[i] = static_cast<std::uint8_t>(
            parse_number(address.substr(0, dot), 255U, "destination IPv4 address"));
        if (!last) {
            address.remove_prefix(dot + 1);
        }
    }
    endpoint.port = static_cast<std::uint16_t>(parse_number(port, 65535U, "UDP port"));
    if (endpoint.port == 0) {
        throw std::runtime_error("UDP port must be greater than zero");
    }
    return endpoint;
}

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
            options.through_sequence = parse_number(
                argv[++index], std::numeric_limits<std::uint64_t>::max(), "MoldUDP64 sequence limit");
        } else {
            throw std::runtime_error("invalid or repeated option: " + std::string(option));
        }
    }
    return options;
}

class Replay {
public:
    std::uint64_t apply_packet(const hft::UdpPacket& udp, std::optional<std::uint64_t> through_sequence) {
        const auto packet = hft::parse_mold(udp.payload);
        const Sender sender{udp.source, udp.source_port, packet.session};
        if (!sender_) {
            if (through_sequence && packet.first_sequence > *through_sequence) {
                throw std::runtime_error("requested sequence precedes the selected capture stream");
            }
            sender_ = sender;
            next_sequence_ = packet.first_sequence;
        } else if (*sender_ != sender) {
            throw std::runtime_error("unexpected MoldUDP64 session or UDP sender");
        }

        if (packet.first_sequence > next_sequence_) {
            throw std::runtime_error("MoldUDP64 sequence gap: expected " +
                std::to_string(next_sequence_) + ", received " + std::to_string(packet.first_sequence));
        }

        if (packet.kind == hft::MoldKind::end_of_session && packet.first_sequence == next_sequence_) {
            if (!itch_ended_) {
                throw std::runtime_error("MoldUDP64 session ended before ITCH End of Messages");
            }
            mold_ended_ = true;
        }

        const auto first_new_sequence = next_sequence_;
        auto blocks = packet.blocks;
        std::uint64_t count = packet.message_count;
        if (through_sequence && count != 0 && packet.first_sequence + count - 1 > *through_sequence) {
            count = *through_sequence - packet.first_sequence + 1;
        }
        for (std::uint64_t i = 0; i < count; ++i) {
            const auto bytes = hft::next_mold_message(blocks);
            const auto sequence = packet.first_sequence + i;
            if (sequence < next_sequence_) {
                continue;
            }
            try {
                apply_message(hft::decode_itch(bytes));
            } catch (const std::runtime_error& error) {
                throw std::runtime_error("sequence " + std::to_string(sequence) + ": " + error.what());
            }
            last_capture_ns_ = udp.capture_ns;
            ++next_sequence_;
        }
        return next_sequence_ - first_new_sequence;
    }

    bool has_reached(std::uint64_t sequence) const {
        return sender_.has_value() && next_sequence_ > sequence;
    }

    void write_snapshot(std::ostream& output, bool stopped_at_sequence) const {
        if (!itch_started_) {
            throw std::runtime_error("no ITCH Start of Messages for the selected feed");
        }
        const auto status = stopped_at_sequence ? "sequence_limit" : mold_ended_ ? "mold_end" :
                            itch_ended_ ? "itch_end" : "capture_prefix";
        output << "REPLAY\t" << (next_sequence_ - 1) << '\t' << last_capture_ns_ << '\t'
               << exchange_ns_ << '\t' << status << '\n';
        book_.write_snapshot(output);
    }

private:
    struct Sender {
        std::array<std::uint8_t, 4> address;
        std::uint16_t port;
        std::array<std::uint8_t, 10> session;
        bool operator==(const Sender&) const = default;
    };

    void apply_message(const hft::ItchMessage& message) {
        if (itch_ended_) {
            throw std::runtime_error("ITCH message after End of Messages");
        }
        if (const auto* seconds = std::get_if<hft::Seconds>(&message.event)) {
            seconds_ = seconds->value;
        } else if (!seconds_) {
            throw std::runtime_error("missing ITCH Seconds message");
        } else if (const auto* system = std::get_if<hft::SystemEvent>(&message.event)) {
            if (system->code == 'O' && !itch_started_) {
                itch_started_ = true;
            } else if (system->code == 'C' && itch_started_) {
                itch_ended_ = true;
            } else {
                throw std::runtime_error("unexpected ITCH System Event");
            }
        } else if (!itch_started_) {
            throw std::runtime_error("missing ITCH Start of Messages");
        }
        exchange_ns_ = static_cast<std::uint64_t>(*seconds_) * 1'000'000'000 + message.nanoseconds;
        book_.apply(message);
    }

    hft::HistoricalBook book_;
    std::optional<Sender> sender_;
    std::optional<std::uint32_t> seconds_;
    std::uint64_t next_sequence_ = 0;
    std::uint64_t last_capture_ns_ = 0;
    std::uint64_t exchange_ns_ = 0;
    bool itch_started_ = false;
    bool itch_ended_ = false;
    bool mold_ended_ = false;
};

}  // namespace

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: hft_replay <capture.pcap> <destination-ipv4> <udp-port>"
                     " [--metrics] [--through-sequence <sequence>]\n";
        return 2;
    }

    try {
        const auto options = parse_options(argc, argv);
        const auto endpoint = parse_endpoint(argv[2], argv[3]);
        std::ifstream input(argv[1], std::ios::binary);
        if (!input) {
            throw std::runtime_error("could not open capture file");
        }
        hft::PcapReader reader(input);
        Replay replay;
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
                const auto messages = replay.apply_packet(packet, options.through_sequence);
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
        replay.write_snapshot(std::cout, stopped_at_sequence);
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
