#include "hft/pcap.hpp"

#include <algorithm>
#include <istream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace hft {
namespace {

constexpr std::size_t ethernet_header_size = 14;
constexpr std::size_t vlan_tag_size = 4;
constexpr std::size_t ipv4_min_header_size = 20;
constexpr std::size_t udp_header_size = 8;
constexpr std::uint32_t max_record_size = 16 * 1024 * 1024;
constexpr std::uint32_t ns_per_second = 1'000'000'000;
constexpr std::uint32_t us_per_second = 1'000'000;

std::uint16_t read_u16(const std::uint8_t* bytes, bool little_endian = false) noexcept {
    const auto high = static_cast<std::uint16_t>(bytes[little_endian ? 1 : 0]);
    const auto low = static_cast<std::uint16_t>(bytes[little_endian ? 0 : 1]);
    return static_cast<std::uint16_t>((high << 8) | low);
}

std::uint32_t read_u32(const std::uint8_t* bytes, bool little_endian = false) noexcept {
    const auto high = read_u16(bytes + (little_endian ? 2 : 0), little_endian);
    const auto low = read_u16(bytes + (little_endian ? 0 : 2), little_endian);
    return (static_cast<std::uint32_t>(high) << 16) | low;
}

std::size_t read_bytes(std::istream& input, std::span<std::uint8_t> buffer) {
    if (buffer.empty()) {
        return 0;
    }
    try {
        input.read(reinterpret_cast<char*>(buffer.data()),
                   static_cast<std::streamsize>(buffer.size()));
    } catch (const std::ios_base::failure&) {
        // The caller reports a short read with the PCAP header or record location.
    }
    return static_cast<std::size_t>(input.gcount());
}

[[noreturn]] void record_error(std::uint64_t index, std::string_view message) {
    throw std::runtime_error("PCAP record " + std::to_string(index) + ": " +
                             std::string(message));
}

bool is_vlan(std::uint16_t ether_type) noexcept {
    return ether_type == 0x8100 || ether_type == 0x88a8 || ether_type == 0x9100;
}

bool decode_udp(std::span<const std::uint8_t> frame, FeedEndpoint endpoint, UdpPacket& packet) {
    if (frame.size() < ethernet_header_size) {
        return false;
    }
    std::uint16_t ether_type = read_u16(frame.data() + 12);
    frame = frame.subspan(ethernet_header_size);
    while (is_vlan(ether_type)) {
        if (frame.size() < vlan_tag_size) {
            return false;
        }
        ether_type = read_u16(frame.data() + 2);
        frame = frame.subspan(vlan_tag_size);
    }
    if (ether_type != 0x0800 || frame.size() < ipv4_min_header_size || frame[9] != 17 ||
        !std::ranges::equal(frame.subspan(16, 4), endpoint.destination)) {
        return false;
    }

    const std::size_t header_length = (frame[0] & 0x0f) * 4U;
    if ((frame[0] >> 4) != 4 || header_length < ipv4_min_header_size) {
        record_error(packet.index, "invalid IPv4 header for the feed destination");
    }
    const std::uint16_t fragment = read_u16(frame.data() + 6);
    if ((fragment & 0x1fff) != 0) {
        record_error(packet.index, "IPv4 fragment for the feed destination has no UDP port");
    }
    if (frame.size() < header_length + 4) {
        record_error(packet.index, "truncated IPv4/UDP header for the feed destination");
    }
    const auto udp = frame.subspan(header_length);
    if (read_u16(udp.data() + 2) != endpoint.port) {
        return false;
    }
    if ((fragment & 0x2000) != 0) {
        record_error(packet.index, "fragmented IPv4 UDP datagram for the feed endpoint");
    }
    if ((fragment & 0x8000) != 0) {
        record_error(packet.index, "invalid IPv4 fragment flags");
    }
    const std::size_t ip_length = read_u16(frame.data() + 2);
    if (ip_length < header_length + udp_header_size || ip_length > frame.size()) {
        record_error(packet.index, "invalid or truncated IPv4 total length");
    }
    const std::size_t udp_length = read_u16(udp.data() + 4);
    if (udp_length != ip_length - header_length) {
        record_error(packet.index, "UDP length does not match the IPv4 payload length");
    }
    std::copy_n(frame.data() + 12, packet.source.size(), packet.source.begin());
    packet.source_port = read_u16(udp.data());
    packet.payload = udp.subspan(udp_header_size, udp_length - udp_header_size);
    return true;
}

} // namespace

PcapReader::PcapReader(std::istream& input) : input_(input) {
    std::array<std::uint8_t, 24> header{};
    if (read_bytes(input_, header) != header.size()) {
        throw std::runtime_error("PCAP file header is truncated or unreadable");
    }
    switch (read_u32(header.data())) {
    case 0xa1b2c3d4:
        break;
    case 0xd4c3b2a1:
        little_endian_ = true;
        break;
    case 0xa1b23c4d:
        nanosecond_precision_ = true;
        break;
    case 0x4d3cb2a1:
        little_endian_ = true;
        nanosecond_precision_ = true;
        break;
    default:
        throw std::runtime_error("unsupported capture format; expected classic PCAP");
    }
    if (read_u16(header.data() + 4, little_endian_) != 2 ||
        read_u16(header.data() + 6, little_endian_) != 4) {
        throw std::runtime_error("unsupported classic PCAP version; expected 2.4");
    }
    snap_length_ = read_u32(header.data() + 16, little_endian_);
    const std::uint32_t link_info = read_u32(header.data() + 20, little_endian_);
    if (snap_length_ == 0 || (link_info & 0x0bff0000) != 0) {
        throw std::runtime_error("invalid PCAP snapshot length or link-layer flags");
    }
    if ((link_info & 0xffff) != 1) {
        throw std::runtime_error("unsupported PCAP link type; expected Ethernet");
    }
    if ((link_info & 0x04000000) != 0) {
        fcs_length_ = (link_info >> 28) * 2;
    }
}

bool PcapReader::next(FeedEndpoint endpoint, UdpPacket& packet) {
    std::array<std::uint8_t, 16> header{};
    while (true) {
        const std::size_t bytes_read = read_bytes(input_, header);
        if (bytes_read == 0 && input_.eof() && !input_.bad()) {
            return false;
        }
        ++record_index_;
        if (bytes_read != header.size()) {
            record_error(record_index_, "truncated or unreadable record header");
        }
        const std::uint32_t seconds = read_u32(header.data(), little_endian_);
        const std::uint32_t fraction = read_u32(header.data() + 4, little_endian_);
        const std::uint32_t captured_length = read_u32(header.data() + 8, little_endian_);
        const std::uint32_t original_length = read_u32(header.data() + 12, little_endian_);
        if (fraction >= (nanosecond_precision_ ? ns_per_second : us_per_second)) {
            record_error(record_index_, "timestamp fraction is outside its precision range");
        }
        if (captured_length > snap_length_ || captured_length > original_length ||
            original_length < fcs_length_) {
            record_error(record_index_, "invalid captured or original packet length");
        }
        if (captured_length > max_record_size) {
            record_error(record_index_, "captured packet exceeds the 16 MiB reader limit");
        }
        frame_.resize(captured_length);
        if (read_bytes(input_, frame_) != frame_.size()) {
            record_error(record_index_, "truncated or unreadable captured packet");
        }
        const std::uint64_t capture_ns = static_cast<std::uint64_t>(seconds) * ns_per_second +
                                         (nanosecond_precision_ ? fraction : fraction * 1'000U);
        const auto ethernet_length = std::min(captured_length, original_length - fcs_length_);
        UdpPacket candidate{record_index_, capture_ns, {}, 0, {}};
        if (decode_udp(std::span<const std::uint8_t>(frame_).first(ethernet_length), endpoint,
                       candidate)) {
            packet = candidate;
            return true;
        }
    }
}

} // namespace hft
