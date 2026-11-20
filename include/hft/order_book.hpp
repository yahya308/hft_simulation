#pragma once

#include "hft/itch.hpp"

#include <cstdint>
#include <iosfwd>
#include <map>
#include <string>
#include <utility>

namespace hft {

class HistoricalBook {
public:
    void apply(const ItchMessage& message);
    void write_snapshot(std::ostream& output) const;

private:
    struct Book {
        Directory directory;
        std::string state;
        std::map<std::pair<std::int32_t, std::int32_t>, std::uint64_t> tick_sizes;
        std::map<OrderKey, Add> orders;
    };

    Book& require_book(std::uint32_t id);

    void apply_event(const Directory& event);
    void apply_event(const TickSize& event);
    void apply_event(const BookState& event);
    void apply_event(const Add& event);
    void apply_event(const Execute& event);
    void apply_event(const Delete& event);
    void apply_event(const Flush& event);
    void apply_event(const Seconds&) {}
    void apply_event(const SystemEvent&) {}
    void apply_event(const BookNotice&) {}

    std::map<std::uint32_t, Book> books_;
};

}  // namespace hft
