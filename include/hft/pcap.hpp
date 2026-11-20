#pragma once

#include <array>
#include <cstdint>
#include <iosfwd>
#include <span>
#include <vector>

namespace hft {

struct FeedEndpoint {
    std::array<std::uint8_t, 4> destination;
    std::uint16_t port;
};

struct UdpPacket {
    std::uint64_t index;
    std::uint64_t capture_ns;
    std::array<std::uint8_t, 4> source;
    std::uint16_t source_port;
    std::span<const std::uint8_t> payload;
};

class PcapReader {
public:
    explicit PcapReader(std::istream& input);

    // The payload references the reader's buffer and is valid until the next call.
    bool next(FeedEndpoint endpoint, UdpPacket& packet);

private:
    std::istream& input_;
    bool little_endian_ = false;
    bool nanosecond_precision_ = false;
    std::uint32_t snap_length_ = 0;
    std::uint32_t fcs_length_ = 0;
    std::uint64_t record_index_ = 0;
    std::vector<std::uint8_t> frame_;
};

} // namespace hft
