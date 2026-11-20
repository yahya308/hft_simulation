#pragma once

#include <array>
#include <cstdint>
#include <span>

namespace hft {

enum class MoldKind { data, heartbeat, end_of_session };

struct MoldPacket {
    std::array<std::uint8_t, 10> session;
    std::uint64_t first_sequence;
    std::uint16_t message_count;
    MoldKind kind;
    std::span<const std::uint8_t> blocks;
};

// The returned blocks borrow the input datagram. Parsing validates the entire frame.
MoldPacket parse_mold(std::span<const std::uint8_t> datagram);

// Advance over one block in a previously validated packet.
std::span<const std::uint8_t> next_mold_message(std::span<const std::uint8_t>& blocks);

}  // namespace hft
