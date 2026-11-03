#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <string>
#include <vector>

namespace premarket::postgres {

struct Config {
    std::string database_url;
    std::string schema;
    std::string table;
    std::string market;
    bool create_table = false;
};

struct Result {
    std::filesystem::path file;
    std::int64_t read = 0;
    std::int64_t upserted = 0;
};

struct Scaled {
    std::map<std::string, std::string> row;
    bool changed = false;
    bool inexact = false;
};

std::string create_table_sql(const std::string& schema, const std::string& table);
std::string upsert_sql(const std::string& schema, const std::string& table);
Scaled to_feed_scale(const std::map<std::string, std::string>& row);
bool spot_fullname(std::map<std::string, std::string>& row);
std::vector<Result> push(const Config& cfg, const std::vector<std::filesystem::path>& files);

}  // namespace premarket::postgres
