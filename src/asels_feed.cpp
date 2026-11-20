#include "hft/itch_stream.hpp"
#include "hft/order_book.hpp"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string_view>
#include <unordered_set>
#include <utility>

namespace {

namespace fs = std::filesystem;

std::string_view equity_ticker(std::string_view symbol) {
    if (symbol.ends_with(".E")) {
        symbol.remove_suffix(2);
    }
    if (!std::ranges::all_of(symbol, [](char value) {
            return (value >= 'A' && value <= 'Z') || (value >= '0' && value <= '9');
        })) {
        return {};
    }
    return symbol;
}

bool supports_equity(const hft::Directory& directory) {
    return directory.financial_product == 5 && directory.ranking_type == 1 &&
           directory.round_lot != 0;
}

std::set<std::string> parse_symbols(std::string_view symbols) {
    std::set<std::string> result;
    while (true) {
        const auto comma = symbols.find(',');
        auto item = symbols.substr(0, comma);
        const auto first = item.find_first_not_of(" \t");
        if (first == std::string_view::npos) {
            throw std::runtime_error("empty symbol in --symbols");
        }
        item = item.substr(first, item.find_last_not_of(" \t") - first + 1);
        std::string upper(item);
        for (auto& value : upper) {
            if (value >= 'a' && value <= 'z') {
                value = static_cast<char>(value - 'a' + 'A');
            }
        }
        const auto ticker = equity_ticker(upper);
        if (ticker.empty() || upper.size() > 32) {
            throw std::runtime_error("invalid equity symbol: " + upper);
        }
        if (!result.emplace(ticker).second) {
            throw std::runtime_error("duplicate equity symbol: " + std::string(ticker));
        }
        if (comma == std::string_view::npos) {
            return result;
        }
        symbols.remove_prefix(comma + 1);
    }
}

void write_json_string(std::ostream& output, std::string_view value) {
    output.put('"');
    for (const char character : value) {
        if (character == '"' || character == '\\') {
            output.put('\\');
        }
        output.put(character);
    }
    output.put('"');
}

void write_yes_no(std::ostream& output, std::optional<bool> value) {
    if (!value.has_value()) {
        output << "null";
        return;
    }
    output << '"' << (*value ? 'Y' : 'N') << '"';
}

class EquityFeed {
public:
    EquityFeed(std::ostream& output, const std::optional<std::set<std::string>>& symbols)
        : output_(output), tagged_(symbols.has_value()) {
        if (symbols) {
            for (const auto& symbol : *symbols) {
                instruments_.emplace(symbol, std::nullopt);
            }
        } else {
            instruments_.emplace("ASELS", std::nullopt);
        }
    }

    void apply(const hft::ItchMessage& message, std::uint64_t sequence, std::uint64_t time) {
        if (message.type == 'S') {
            begin_record(message.type, sequence, time);
            output_ << ",\"" << std::get<hft::SystemEvent>(message.event).code << "\"]\n";
            return;
        }
        if (const auto* directory = std::get_if<hft::Directory>(&message.event)) {
            apply_directory(message, *directory, sequence, time);
            return;
        }
        switch (message.type) {
        case 'L': {
            const auto& event = std::get<hft::TickSize>(message.event);
            if (!books_.contains(event.book)) return;
            book_.apply(message);
            begin_record(message.type, sequence, time, event.book);
            output_ << ',' << event.from_price << ',' << event.to_price << ',' << event.tick_size;
            break;
        }
        case 'O': {
            const auto& event = std::get<hft::BookState>(message.event);
            if (!books_.contains(event.book)) return;
            book_.apply(message);
            begin_record(message.type, sequence, time, event.book);
            output_.put(',');
            write_json_string(output_, event.state);
            break;
        }
        case 'A': {
            const auto& event = std::get<hft::Add>(message.event);
            if (!books_.contains(event.key.book)) return;
            book_.apply(message);
            begin_order(message.type, sequence, time, event.key);
            output_ << ',' << event.price << ',' << event.quantity << ','
                    << event.rank_time << ',' << event.rank_sequence;
            break;
        }
        case 'E':
        case 'C': {
            const auto& event = std::get<hft::Execute>(message.event);
            if (!books_.contains(event.key.book)) return;
            book_.apply(message);
            begin_order(message.type, sequence, time, event.key);
            output_ << ',' << event.quantity << ',' << event.match_id;
            if (event.priced) {
                output_ << ',' << event.priced->trade_price << ',';
                write_yes_no(output_, event.priced->at_cross);
                output_.put(',');
                write_yes_no(output_, event.priced->printable);
            }
            break;
        }
        case 'D': {
            const auto& event = std::get<hft::Delete>(message.event);
            if (!books_.contains(event.key.book)) return;
            book_.apply(message);
            begin_order(message.type, sequence, time, event.key);
            break;
        }
        case 'Y': {
            const auto& event = std::get<hft::Flush>(message.event);
            if (!books_.contains(event.book)) return;
            book_.apply(message);
            begin_record(message.type, sequence, time, event.book);
            break;
        }
        default:
            return;
        }
        output_ << "]\n";
    }

    void require_instruments() const {
        for (const auto& [symbol, book] : instruments_) {
            if (!book) {
                throw std::runtime_error(symbol + " directory was not found in the selected Pay Piyasasi stream");
            }
        }
    }

    std::uint64_t records() const { return records_; }

private:
    void apply_directory(const hft::ItchMessage& message, const hft::Directory& directory,
                         std::uint64_t sequence, std::uint64_t time) {
        const auto ticker = equity_ticker(directory.symbol);
        const auto selected = tagged_ && !supports_equity(directory) ? instruments_.end()
                                                                    : instruments_.find(ticker);
        if (books_.contains(directory.book) &&
            (selected == instruments_.end() || selected->second != directory.book)) {
            throw std::runtime_error("selected order book was assigned to another instrument");
        }
        if (selected == instruments_.end()) {
            return;
        }
        if (selected->second && selected->second != directory.book) {
            throw std::runtime_error("multiple " + selected->first + " order books in the selected stream");
        }
        book_.apply(message);
        selected->second = directory.book;
        books_.insert(directory.book);
        begin_record(message.type, sequence, time, directory.book);
        if (!tagged_) {
            output_ << ',' << directory.book;
        }
        output_.put(',');
        write_json_string(output_, directory.symbol);
        output_ << ',' << directory.price_decimals << ',' << directory.round_lot << "]\n";
    }

    void begin_record(char type, std::uint64_t sequence, std::uint64_t time,
                      std::optional<std::uint32_t> book = std::nullopt) {
        ++records_;
        output_ << "[\"" << type << "\"," << sequence << ',' << time;
        if (tagged_) {
            output_.put(',');
            if (book) {
                output_ << *book;
            } else {
                output_ << "null";
            }
        }
    }

    void begin_order(char type, std::uint64_t sequence, std::uint64_t time, const hft::OrderKey& key) {
        begin_record(type, sequence, time, key.book);
        output_ << ',' << key.id << ",\"" << (key.side == hft::Side::buy ? 'B' : 'S') << '"';
    }

    std::ostream& output_;
    bool tagged_;
    hft::HistoricalBook book_;
    std::map<std::string, std::optional<std::uint32_t>, std::less<>> instruments_;
    std::unordered_set<std::uint32_t> books_;
    std::uint64_t records_ = 0;
};

class StagedOutput {
public:
    explicit StagedOutput(fs::path destination)
        : destination_(std::move(destination)), directory_(destination_), file_(destination_) {
        directory_ += ".partial";
        file_ = directory_ / "events.ndjson";
        if (!fs::create_directory(directory_)) {
            throw std::runtime_error("ASELS extraction output is already being prepared");
        }
    }

    StagedOutput(const StagedOutput&) = delete;
    StagedOutput& operator=(const StagedOutput&) = delete;

    ~StagedOutput() {
        std::error_code ignored;
        fs::remove(file_, ignored);
        fs::remove(directory_, ignored);
    }

    const fs::path& file() const { return file_; }
    void publish() const { fs::rename(file_, destination_); }

private:
    fs::path destination_;
    fs::path directory_;
    fs::path file_;
};

void extract(const fs::path& capture, hft::FeedEndpoint endpoint, const fs::path& destination,
             std::optional<std::uint64_t> through_sequence,
             const std::optional<std::set<std::string>>& symbols) {
    if (fs::exists(destination) && fs::equivalent(capture, destination)) {
        throw std::runtime_error("NDJSON output must be different from the input PCAP");
    }
    std::ifstream input(capture, std::ios::binary);
    if (!input) {
        throw std::runtime_error("could not open capture file");
    }
    hft::PcapReader reader(input);
    const StagedOutput stage(destination);
    std::ofstream output(stage.file(), std::ios::binary);
    if (!output) {
        throw std::runtime_error("could not open ASELS extraction output");
    }

    EquityFeed feed(output, symbols);
    hft::ItchStream stream;
    hft::UdpPacket packet{};
    unsigned packets_since_progress = 0;
    bool stopped_at_sequence = false;
    while (reader.next(endpoint, packet)) {
        try {
            stream.read_packet(packet, through_sequence,
                [&feed](const hft::ItchMessage& message, std::uint64_t sequence, std::uint64_t time) {
                    feed.apply(message, sequence, time);
                });
        } catch (const std::runtime_error& error) {
            throw std::runtime_error("packet " + std::to_string(packet.index) + ": " + error.what());
        }
        if (through_sequence && stream.has_reached(*through_sequence)) {
            stopped_at_sequence = true;
            break;
        }
        if (++packets_since_progress == 250'000) {
            std::cerr << "PROGRESS\t" << input.tellg() << '\n';
            packets_since_progress = 0;
        }
    }
    if (through_sequence && !stopped_at_sequence) {
        throw std::runtime_error("capture ended before the requested sequence");
    }
    stream.require_started();
    feed.require_instruments();
    output.close();
    if (!output) {
        throw std::runtime_error("could not write ASELS extraction output");
    }
    stage.publish();
    std::cerr << "EXTRACT\trecords=" << feed.records() << "\tlast_sequence=" << stream.last_sequence()
              << "\tstatus=" << stream.status(stopped_at_sequence) << '\n';
}

void list_symbols(const fs::path& capture, hft::FeedEndpoint endpoint, std::ostream& output) {
    std::ifstream input(capture, std::ios::binary);
    if (!input) {
        throw std::runtime_error("could not open capture file");
    }
    hft::PcapReader reader(input);
    hft::ItchStream stream;
    hft::UdpPacket packet{};
    std::map<std::string, hft::Directory, std::less<>> instruments;
    std::unordered_set<std::uint32_t> books;
    bool opening_complete = false;
    const auto collect = [&](const hft::ItchMessage& message, std::uint64_t, std::uint64_t) {
        if (opening_complete) {
            return;
        }
        // Opening reference data precedes book state and trading activity in this feed profile.
        constexpr std::string_view opening_types = "TSRLMV";
        const auto* system = std::get_if<hft::SystemEvent>(&message.event);
        opening_complete = opening_types.find(message.type) == std::string_view::npos ||
                           (system && system->code == 'C');
        const auto* directory = std::get_if<hft::Directory>(&message.event);
        if (!directory || !supports_equity(*directory)) {
            return;
        }
        const auto ticker = equity_ticker(directory->symbol);
        if (ticker.empty()) {
            return;
        }
        auto [entry, inserted] = instruments.try_emplace(std::string(ticker), *directory);
        if (entry->second.book != directory->book || (inserted && !books.insert(directory->book).second)) {
            throw std::runtime_error("ambiguous opening order book for " + std::string(ticker));
        }
        if (!inserted) {
            entry->second = *directory;
        }
    };
    while (!opening_complete && reader.next(endpoint, packet)) {
        try {
            stream.read_packet(packet, std::nullopt, collect);
        } catch (const std::runtime_error& error) {
            throw std::runtime_error("packet " + std::to_string(packet.index) + ": " + error.what());
        }
    }
    stream.require_started();

    output.put('[');
    bool first = true;
    for (const auto& entry : instruments) {
        const auto& directory = entry.second;
        if (!first) {
            output.put(',');
        }
        first = false;
        output << "{\"symbol\":";
        write_json_string(output, directory.symbol);
        output << ",\"book_id\":" << directory.book << '}';
    }
    output << "]\n";
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 5 || (argc - 5) % 2 != 0) {
        std::cerr << "Usage: hft_asels_feed <capture.pcap> <destination-ipv4> <udp-port>"
                     " <output.ndjson> [--symbols <symbol,...>] [--through-sequence <sequence>]\n"
                     "       hft_asels_feed <capture.pcap> <destination-ipv4> <udp-port> --list-symbols\n";
        return 2;
    }
    try {
        const auto endpoint = hft::parse_feed_endpoint(argv[2], argv[3]);
        if (std::string_view(argv[4]) == "--list-symbols") {
            if (argc != 5) {
                throw std::runtime_error("--list-symbols does not accept extraction options");
            }
            list_symbols(argv[1], endpoint, std::cout);
            return 0;
        }
        std::optional<std::uint64_t> through_sequence;
        std::optional<std::set<std::string>> symbols;
        for (int index = 5; index < argc; index += 2) {
            const std::string_view option(argv[index]);
            if (option == "--through-sequence" && !through_sequence) {
                through_sequence = hft::parse_sequence_limit(argv[index + 1]);
            } else if (option == "--symbols" && !symbols) {
                symbols = parse_symbols(argv[index + 1]);
            } else {
                throw std::runtime_error("invalid or repeated extraction option: " + std::string(option));
            }
        }
        extract(argv[1], endpoint, argv[4], through_sequence, symbols);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ASELS extraction failed: " << error.what() << '\n';
        return 1;
    }
}
