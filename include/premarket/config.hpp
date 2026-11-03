#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace premarket::config {

namespace fs = std::filesystem;

inline constexpr int CONFIG_VERSION = 8;
inline constexpr const char* VENUE_PREFIX = "venue.";

struct Paths {
    fs::path data_dir;
    fs::path contracts_dir;
    fs::path logs_dir;
    fs::path reports_dir;
    fs::path baskets_dir;
    fs::path sinks_dir;
};

struct Fyers {
    std::string base_url = "https://public.fyers.in/sym_details";
    std::string user_agent =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36";
    std::int64_t timeout_sec = 120;
    std::int64_t retries = 3;
    std::int64_t retry_delay_sec = 2;
};

struct Venue {
    std::string mic;
    std::string feed;
    bool enabled = true;
    std::int64_t venue_id = 0;
    std::string dataset;
    std::string stype_in = "raw_symbol";
    fs::path symbols;
};

struct Config {
    fs::path file;
    Paths paths;
    std::string timezone;
    bool baskets_enabled = true;
    Fyers fyers;
    std::map<std::string, Venue> venues;
};

Config load(const fs::path& path);
std::map<std::string, std::string> load_keys(const fs::path& path);

void init(const fs::path& config_path, const fs::path& keys_path = {});
void install(Config cfg, std::map<std::string, std::string> keys = {}, fs::path keys_path = {});
bool loaded();
const Config& get();
const fs::path& keys_path();
std::string databento_key(const std::string& mic);

const Venue* venue(const std::string& mic);
std::vector<const Venue*> venues_of_feed(const std::string& feed);
std::string feed_for(const std::string& mic);
bool owns(const std::string& mic, const std::string& feed);
std::string today();

}  // namespace premarket::config
