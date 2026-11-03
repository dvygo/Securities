#pragma once

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "premarket/util.hpp"

namespace premarket::load {

inline constexpr const char* CLICKHOUSE_NORMAL = "clickhouse-normal";
inline constexpr const char* POSTGRES_PLUGIN = "postgres-plugin";
inline constexpr const char* MDF_TOKENMAP = "mdf-tokenmap";

struct SinkError : ValueError {
    using ValueError::ValueError;
};

struct Sink {
    std::string name;
    std::string type;
    std::map<std::string, std::string> settings;
    std::filesystem::path path;

    std::string get(const std::string& key, const std::string& fallback = "") const;
    std::string require(const std::string& key) const;
};

std::vector<std::string> available();
Sink load_sink(const std::string& name);
int run(const std::string& date_dir, const std::vector<std::string>& names, bool dry_run);

}  // namespace premarket::load
