#pragma once

#include <compare>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <variant>

namespace hft {

enum class Side { buy, sell };

struct OrderKey {
    std::uint32_t book;
    Side side;
    std::uint64_t id;

    auto operator<=>(const OrderKey&) const = default;
};

struct Directory {
    std::uint32_t book;
    std::string symbol;
    std::uint16_t price_decimals;
    std::uint32_t round_lot;
    std::uint8_t ranking_type;
    std::uint8_t financial_product;
};

struct TickSize {
    std::uint32_t book;
    std::uint64_t tick_size;
    std::int32_t from_price;
    std::int32_t to_price;
};

struct BookState {
    std::uint32_t book;
    std::string state;
};

struct Add {
    OrderKey key;
    std::uint32_t rank_sequence;
    std::uint64_t quantity;
    std::int32_t price;
    std::uint64_t rank_time;
};

struct PricedExecution {
    std::int32_t trade_price;
    std::optional<bool> at_cross;
    std::optional<bool> printable;
};

struct Execute {
    OrderKey key;
    std::uint64_t quantity;
    std::uint64_t match_id;
    std::optional<PricedExecution> priced;
};

struct Delete {
    OrderKey key;
};

struct Flush {
    std::uint32_t book;
};

struct Seconds {
    std::uint32_t value;
};

struct SystemEvent {
    char code;
};

struct BookNotice {
    std::uint32_t book;
};

struct ItchMessage {
    char type;
    std::uint32_t nanoseconds;
    std::variant<Seconds, SystemEvent, Directory, TickSize, BookState, Add, Execute,
                 Delete, Flush, BookNotice> event;
};

ItchMessage decode_itch(std::span<const std::uint8_t> bytes);

}  // namespace hft
