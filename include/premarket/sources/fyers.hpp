#pragma once

#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "premarket/config.hpp"
#include "premarket/runner.hpp"

namespace premarket::fyers {

using Row = std::unordered_map<std::string, std::string>;

extern const std::vector<std::string> FYERS_COLUMNS;
extern const std::map<std::string, std::string> LEGACY_HEADER_ALIASES;
extern const std::map<int, std::string> EXCHANGE_CODES;
extern const std::map<int, std::string> SEGMENT_CODES;
extern const std::map<int, std::string> INSTRUMENT_CODES;

std::string normalize_header_key(const std::string& key);
std::string resolve_exchange_mic(const std::string& exchange, const std::string& segment);
std::vector<Row> read_raw_csv(const std::filesystem::path& path);
std::vector<Row> parse_fyers_csv(const std::filesystem::path& path);

std::string fetch(const std::string& url, const config::Fyers& cfg);
void download(runner::Opts& opts);

}  // namespace premarket::fyers
