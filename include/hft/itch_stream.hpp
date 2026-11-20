#pragma once

#include "hft/itch.hpp"
#include "hft/moldudp.hpp"
#include "hft/pcap.hpp"

#include <charconv>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string_view>

namespace hft {

template <typename Number>
Number parse_bounded_number(std::string_view text, Number maximum, const char* name) {
    Number value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size() || value > maximum) {
        throw std::runtime_error(std::string("invalid ") + name);
    }
    return value;
}

inline FeedEndpoint parse_feed_endpoint(std::string_view address, std::string_view port) {
    FeedEndpoint endpoint{};
    for (std::size_t i = 0; i < endpoint.destination.size(); ++i) {
        const auto dot = address.find('.');
        const bool last = i + 1 == endpoint.destination.size();
        if (last != (dot == std::string_view::npos)) {
            throw std::runtime_error("invalid destination IPv4 address");
        }
        endpoint.destination[i] = static_cast<std::uint8_t>(
            parse_bounded_number(address.substr(0, dot), 255U, "destination IPv4 address"));
        if (!last) {
            address.remove_prefix(dot + 1);
        }
    }
    endpoint.port = static_cast<std::uint16_t>(parse_bounded_number(port, 65535U, "UDP port"));
    if (endpoint.port == 0) {
        throw std::runtime_error("UDP port must be greater than zero");
    }
    return endpoint;
}

inline std::uint64_t parse_sequence_limit(std::string_view text) {
    return parse_bounded_number(text, std::numeric_limits<std::uint64_t>::max(), "MoldUDP64 sequence limit");
}

class ItchStream {
public:
    template <typename Apply>
    std::uint64_t read_packet(const UdpPacket& udp, std::optional<std::uint64_t> through_sequence, Apply&& apply) {
        const auto packet = parse_mold(udp.payload);
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
        if (packet.kind == MoldKind::end_of_session && packet.first_sequence == next_sequence_) {
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
            const auto bytes = next_mold_message(blocks);
            const auto sequence = packet.first_sequence + i;
            if (sequence < next_sequence_) {
                continue;
            }
            try {
                const auto message = decode_itch(bytes);
                read_time(message);
                apply(message, sequence, exchange_ns_);
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

    void require_started() const {
        if (!itch_started_) {
            throw std::runtime_error("no ITCH Start of Messages for the selected feed");
        }
    }

    std::uint64_t last_sequence() const { return next_sequence_ - 1; }
    std::uint64_t last_capture_ns() const { return last_capture_ns_; }
    std::uint64_t exchange_ns() const { return exchange_ns_; }

    const char* status(bool stopped_at_sequence) const {
        if (stopped_at_sequence) {
            return "sequence_limit";
        }
        return mold_ended_ ? "mold_end" : itch_ended_ ? "itch_end" : "capture_prefix";
    }

private:
    struct Sender {
        std::array<std::uint8_t, 4> address;
        std::uint16_t port;
        std::array<std::uint8_t, 10> session;
        bool operator==(const Sender&) const = default;
    };

    void read_time(const ItchMessage& message) {
        if (itch_ended_) {
            throw std::runtime_error("ITCH message after End of Messages");
        }
        if (const auto* seconds = std::get_if<Seconds>(&message.event)) {
            seconds_ = seconds->value;
        } else if (!seconds_) {
            throw std::runtime_error("missing ITCH Seconds message");
        } else if (const auto* system = std::get_if<SystemEvent>(&message.event)) {
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
    }

    std::optional<Sender> sender_;
    std::optional<std::uint32_t> seconds_;
    std::uint64_t next_sequence_ = 0;
    std::uint64_t last_capture_ns_ = 0;
    std::uint64_t exchange_ns_ = 0;
    bool itch_started_ = false;
    bool itch_ended_ = false;
    bool mold_ended_ = false;
};

} // namespace hft
