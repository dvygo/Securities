#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <databento/record.hpp>

#include "premarket/config.hpp"
#include "premarket/runner.hpp"

namespace premarket::databento_src {

inline constexpr const char* ALL_SYMBOLS = "ALL_SYMBOLS";
inline constexpr int LIVE_DEFINITION_REPLAY_CEILING_SEC = 15 * 60;
inline constexpr std::uint64_t UNDEF_TIMESTAMP = 18446744073709551615ULL;

std::string dataset_key(const std::string& dataset);
std::vector<std::string> resolve_symbols(const config::Venue& venue, const std::optional<std::string>& symbols_file);

struct Selection {
    std::vector<databento::InstrumentDefMsg> kept;
    std::size_t distinct = 0;
};
Selection select_live_definitions(const std::vector<databento::InstrumentDefMsg>& records, std::int64_t day_start_ns);

std::string encode_definition_file(const std::vector<databento::InstrumentDefMsg>& records, const std::string& dataset,
                                   const std::string& stype_in, const std::vector<std::string>& symbols,
                                   std::int64_t start_ns, std::int64_t end_ns);
std::string list_repr(const std::vector<std::string>& items);

void download(runner::Opts& opts, const std::string& venue);

}  // namespace premarket::databento_src
