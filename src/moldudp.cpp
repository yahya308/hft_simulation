// Datagram framing adapted from FastMM's parse_packet. See THIRD_PARTY_NOTICES.md.
#include "hft/moldudp.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace hft {
namespace {

constexpr std::size_t header_size = 20;
constexpr std::uint16_t end_of_session = 0xffff;

std::uint16_t read_length(std::span<const std::uint8_t> bytes) {
    return static_cast<std::uint16_t>((bytes[0] << 8) | bytes[1]);
}

}  // namespace

MoldPacket parse_mold(std::span<const std::uint8_t> datagram) {
    if (datagram.size() < header_size) {
        throw std::runtime_error("truncated MoldUDP64 header");
    }

    MoldPacket packet{};
    std::copy_n(datagram.begin(), packet.session.size(), packet.session.begin());
    for (const auto byte : datagram.subspan(10, 8)) {
        packet.first_sequence = (packet.first_sequence << 8) | byte;
    }
    const auto wire_count = read_length(datagram.subspan(18, 2));
    const auto blocks = datagram.subspan(header_size);
    if (wire_count == 0 || wire_count == end_of_session) {
        if (!blocks.empty()) {
            throw std::runtime_error("unexpected payload on MoldUDP64 control packet");
        }
        packet.kind = wire_count == 0 ? MoldKind::heartbeat : MoldKind::end_of_session;
        return packet;
    }

    if (packet.first_sequence > std::numeric_limits<std::uint64_t>::max() - wire_count) {
        throw std::runtime_error("MoldUDP64 sequence overflow");
    }
    auto remaining = blocks;
    for (std::uint16_t i = 0; i < wire_count; ++i) {
        if (remaining.size() < 2) {
            throw std::runtime_error("missing MoldUDP64 message length");
        }
        const auto length = read_length(remaining);
        if (length == 0 || length > remaining.size() - 2) {
            throw std::runtime_error("invalid ITCH block length in MoldUDP64 packet");
        }
        remaining = remaining.subspan(2 + length);
    }
    if (!remaining.empty()) {
        throw std::runtime_error("trailing bytes in MoldUDP64 packet");
    }
    packet.kind = MoldKind::data;
    packet.message_count = wire_count;
    packet.blocks = blocks;
    return packet;
}

std::span<const std::uint8_t> next_mold_message(std::span<const std::uint8_t>& blocks) {
    const auto length = read_length(blocks);
    const auto message = blocks.subspan(2, length);
    blocks = blocks.subspan(2 + length);
    return message;
}

}  // namespace hft
