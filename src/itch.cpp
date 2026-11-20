#include "hft/itch.hpp"

#include <bit>
#include <cstddef>
#include <stdexcept>

namespace hft {
namespace {

template <typename Integer>
Integer read_big_endian(std::span<const std::uint8_t> bytes, std::size_t offset) {
    std::uint64_t value = 0;
    for (std::size_t index = 0; index < sizeof(Integer); ++index) {
        value = (value << 8) | bytes[offset + index];
    }
    return static_cast<Integer>(value);
}

std::int32_t read_price(std::span<const std::uint8_t> bytes, std::size_t offset) {
    return std::bit_cast<std::int32_t>(read_big_endian<std::uint32_t>(bytes, offset));
}

std::string read_alpha(std::span<const std::uint8_t> bytes, std::size_t offset,
                       std::size_t width) {
    while (width != 0 && bytes[offset + width - 1] == ' ') {
        --width;
    }
    std::string result;
    result.reserve(width);
    for (const auto byte : bytes.subspan(offset, width)) {
        if (byte < 0x20 || (byte >= 0x7f && byte < 0xa0)) {
            throw std::runtime_error("Invalid control byte in ITCH Alpha field");
        }
        if (byte >= 0xa0) {
            result.push_back(static_cast<char>(0xc0 | (byte >> 6)));
            result.push_back(static_cast<char>(0x80 | (byte & 0x3f)));
        } else {
            result.push_back(static_cast<char>(byte));
        }
    }
    return result;
}

void require_length(std::span<const std::uint8_t> bytes, std::size_t expected) {
    if (bytes.size() != expected) {
        throw std::runtime_error("Invalid ITCH length for message type " +
                                 std::string(1, static_cast<char>(bytes.front())) + ": expected " +
                                 std::to_string(expected) + ", received " + std::to_string(bytes.size()));
    }
}

std::uint32_t read_nanoseconds(std::span<const std::uint8_t> bytes, std::size_t length) {
    require_length(bytes, length);
    const auto nanoseconds = read_big_endian<std::uint32_t>(bytes, 1);
    if (nanoseconds >= 1'000'000'000) {
        throw std::runtime_error("Invalid ITCH nanoseconds within second");
    }
    return nanoseconds;
}

OrderKey read_order_key(std::span<const std::uint8_t> bytes) {
    if (bytes[17] != 'B' && bytes[17] != 'S') {
        throw std::runtime_error("Invalid ITCH order side");
    }
    return {
        read_big_endian<std::uint32_t>(bytes, 13),
        bytes[17] == 'B' ? Side::buy : Side::sell,
        read_big_endian<std::uint64_t>(bytes, 5),
    };
}

}  // namespace

ItchMessage decode_itch(std::span<const std::uint8_t> bytes) {
    if (bytes.empty()) {
        throw std::runtime_error("Empty ITCH message");
    }

    const auto type = static_cast<char>(bytes.front());
    switch (type) {
    case 'T':
        require_length(bytes, 5);
        return {type, 0, Seconds{read_big_endian<std::uint32_t>(bytes, 1)}};
    case 'S': {
        const auto nanoseconds = read_nanoseconds(bytes, 6);
        const auto code = static_cast<char>(bytes[5]);
        if (code != 'O' && code != 'C') {
            throw std::runtime_error("Invalid ITCH system event code");
        }
        return {type, nanoseconds, SystemEvent{code}};
    }
    case 'R':
        return {type, read_nanoseconds(bytes, 130), Directory{
            read_big_endian<std::uint32_t>(bytes, 5),
            read_alpha(bytes, 9, 32),
            read_big_endian<std::uint16_t>(bytes, 89),
            read_big_endian<std::uint32_t>(bytes, 97),
            bytes[129],
        }};
    case 'L':
        return {type, read_nanoseconds(bytes, 25), TickSize{
            read_big_endian<std::uint32_t>(bytes, 5),
            read_big_endian<std::uint64_t>(bytes, 9),
            read_price(bytes, 17),
            read_price(bytes, 21),
        }};
    case 'O':
        return {type, read_nanoseconds(bytes, 29), BookState{
            read_big_endian<std::uint32_t>(bytes, 5),
            read_alpha(bytes, 9, 20),
        }};
    case 'A':
        return {type, read_nanoseconds(bytes, 45), Add{
            read_order_key(bytes),
            read_big_endian<std::uint32_t>(bytes, 18),
            read_big_endian<std::uint64_t>(bytes, 22),
            read_price(bytes, 30),
            read_big_endian<std::uint64_t>(bytes, 37),
        }};
    case 'E':
    case 'C':
        return {type, read_nanoseconds(bytes, type == 'E' ? 52 : 58), Execute{
            read_order_key(bytes),
            read_big_endian<std::uint64_t>(bytes, 18),
        }};
    case 'D':
        return {type, read_nanoseconds(bytes, 18), Delete{read_order_key(bytes)}};
    case 'Y':
        return {type, read_nanoseconds(bytes, 9), Flush{read_big_endian<std::uint32_t>(bytes, 5)}};
    case 'P':
        return {type, read_nanoseconds(bytes, 50), BookNotice{read_big_endian<std::uint32_t>(bytes, 26)}};
    case 'M':
        return {type, read_nanoseconds(bytes, 18), BookNotice{read_big_endian<std::uint32_t>(bytes, 5)}};
    case 'V':
        return {type, read_nanoseconds(bytes, 10), BookNotice{read_big_endian<std::uint32_t>(bytes, 5)}};
    case 'Z':
        return {type, read_nanoseconds(bytes, 53), BookNotice{read_big_endian<std::uint32_t>(bytes, 5)}};
    default:
        throw std::runtime_error("Unsupported ITCH message type byte " + std::to_string(bytes.front()));
    }
}

}  // namespace hft
