#pragma once

#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace premarket::paths {

namespace fs = std::filesystem;

fs::path data_root();
fs::path reports_dir();
fs::path sinks_dir();

inline constexpr const char* TRANSFORM_DIR = "TRANSFORM";

fs::path day_dir(const std::string& as_of);
fs::path venue_dir(const std::string& as_of, const std::string& venue_name);
fs::path manual_venue_dir(const std::string& as_of, const std::string& venue_name);
fs::path fyers_segment_path(const std::string& as_of, const std::string& segment);
fs::path normalized_dir(const std::string& as_of);
fs::path plugin_dir(const std::string& as_of);
fs::path baskets_dir();
fs::path contracts_dir();
fs::path contracts_day_dir(const std::string& as_of);
fs::path logs_dir();
void ensure_log_dir();
std::string validate_date_dir(const std::string& date_dir);
void promote_staging(const fs::path& temp_path, const fs::path& output_path);

extern const std::vector<std::string> NEXUS_BASKET_COLUMNS;
extern const std::vector<std::string> DEFINITION_FIELDS;
inline constexpr const char* DEFINITION_COLUMN_PREFIX = "def_";
extern const std::vector<std::string> DEFINITION_PASSTHROUGH_COLUMNS;
extern const std::vector<std::string> ENABLE_COLUMNS;
extern const std::vector<std::string> NORMALIZED_COLUMNS;
extern const std::vector<std::string> CONTRACT_COLUMNS;

struct FyersSegment {
    std::string segment;
    std::string vendor_file;
    std::string local_file;
};
extern const std::vector<FyersSegment> FYERS_RAW_SEGMENTS;

struct FyersBundle {
    std::string mic;
    std::string output;
    std::string table;
    std::vector<std::string> sources;
};
extern const std::vector<FyersBundle> FYERS_MIC_BUNDLES;

const std::map<std::string, std::map<std::string, std::string>>& feed_outputs();
std::string fyers_segment_mic(const std::string& segment);
const FyersSegment& fyers_segment(const std::string& segment);

struct FuturesSource {
    std::string name;
    std::string source;
    bool near_only;
};
extern const std::vector<FuturesSource> INDEX_FUTURES_SOURCES;
extern const std::vector<FuturesSource> EQUITY_FUTURES_SOURCES;
extern const std::vector<std::pair<std::string, std::string>> EQUITY_BASKETS;

struct OptionBasketSource {
    std::string name;
    std::string futures;
    std::string mic;
    bool near_only;
};
extern const std::vector<OptionBasketSource> OPTION_BASKET_SOURCES;
extern const std::vector<std::string> ALL_INDEX_FUTURES_PARTS;
const std::vector<std::string>& basket_names();
std::string basket_venue(const std::string& basket);
std::vector<std::string> basket_names_of(const std::string& venue);
std::string options_basket_name(const std::string& futures);

}  // namespace premarket::paths
