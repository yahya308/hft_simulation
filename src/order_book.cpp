#include "hft/order_book.hpp"

#include <algorithm>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <tuple>
#include <variant>
#include <vector>

namespace hft {
namespace {

constexpr std::uint8_t price_time_ranking = 1;
constexpr auto market_order_price = std::numeric_limits<std::int32_t>::min();

char side_code(Side side) {
    return side == Side::buy ? 'B' : 'S';
}

std::string describe_order(const OrderKey& key) {
    return "book " + std::to_string(key.book) + " side " + side_code(key.side) +
           " order " + std::to_string(key.id);
}

bool has_priority(const Add* lhs, const Add* rhs) {
    if (lhs->key.side != rhs->key.side) {
        return lhs->key.side == Side::buy;
    }
    if (lhs->price != rhs->price) {
        return lhs->key.side == Side::buy ? lhs->price > rhs->price : lhs->price < rhs->price;
    }
    return std::tie(lhs->rank_time, lhs->rank_sequence, lhs->key.id) <
           std::tie(rhs->rank_time, rhs->rank_sequence, rhs->key.id);
}

}  // namespace

void HistoricalBook::apply(const ItchMessage& message) {
    std::visit([this](const auto& event) { apply_event(event); }, message.event);
}

HistoricalBook::Book& HistoricalBook::require_book(std::uint32_t id) {
    const auto book = books_.find(id);
    if (book == books_.end()) {
        throw std::runtime_error("unknown order book " + std::to_string(id));
    }
    return book->second;
}

void HistoricalBook::apply_event(const Directory& event) {
    if (event.ranking_type != price_time_ranking) {
        throw std::runtime_error("unsupported ranking type " + std::to_string(event.ranking_type) +
                                 " for book " + std::to_string(event.book));
    }
    books_[event.book].directory = event;
}

void HistoricalBook::apply_event(const TickSize& event) {
    require_book(event.book).tick_sizes.insert_or_assign({event.from_price, event.to_price}, event.tick_size);
}

void HistoricalBook::apply_event(const BookState& event) {
    require_book(event.book).state = event.state;
}

void HistoricalBook::apply_event(const Add& event) {
    if (!require_book(event.key.book).orders.emplace(event.key, event).second) {
        throw std::runtime_error("duplicate active order for " + describe_order(event.key));
    }
}

void HistoricalBook::apply_event(const Execute& event) {
    auto& orders = require_book(event.key.book).orders;
    const auto order = orders.find(event.key);
    if (order == orders.end()) {
        throw std::runtime_error("execution for unknown order in " + describe_order(event.key));
    }
    auto& remaining = order->second.quantity;
    if (event.quantity > remaining) {
        throw std::runtime_error("executed quantity exceeds remaining quantity for " + describe_order(event.key));
    }
    if (event.quantity == 0) {
        return;
    }
    remaining -= event.quantity;
    if (remaining == 0) {
        orders.erase(order);
    }
}

void HistoricalBook::apply_event(const Delete& event) {
    if (require_book(event.key.book).orders.erase(event.key) == 0) {
        throw std::runtime_error("delete for unknown order in " + describe_order(event.key));
    }
}

void HistoricalBook::apply_event(const Flush& event) {
    require_book(event.book).orders.clear();
}

void HistoricalBook::write_snapshot(std::ostream& output) const {
    for (const auto& [id, book] : books_) {
        const auto& directory = book.directory;
        output << "BOOK\t" << id << '\t' << directory.symbol << '\t' << directory.price_decimals
               << '\t' << directory.round_lot << '\t' << static_cast<unsigned>(directory.ranking_type)
               << '\t' << book.state << '\n';

        for (const auto& [range, tick_size] : book.tick_sizes) {
            output << "TICK\t" << id << '\t' << range.first << '\t' << range.second << '\t' << tick_size << '\n';
        }

        std::vector<const Add*> visible_orders;
        visible_orders.reserve(book.orders.size());
        for (const auto& [key, order] : book.orders) {
            if (order.quantity != 0 && order.price != market_order_price) {
                visible_orders.push_back(&order);
            }
        }
        std::sort(visible_orders.begin(), visible_orders.end(), has_priority);
        for (const auto* order : visible_orders) {
            output << "ORDER\t" << id << '\t' << side_code(order->key.side) << '\t' << order->price
                   << '\t' << order->rank_time << '\t' << order->rank_sequence << '\t' << order->key.id
                   << '\t' << order->quantity << '\n';
        }
    }
}

}  // namespace hft
