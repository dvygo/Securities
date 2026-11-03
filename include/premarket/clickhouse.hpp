#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "premarket/jsonio.hpp"

namespace premarket::clickhouse {

inline constexpr std::size_t EXPORT_BATCH_ROWS = 5000;
inline constexpr std::size_t BASKET_BATCH_ROWS = 1;
inline constexpr const char* COMPACT_PARTS_SETTINGS = "min_bytes_for_wide_part = 1073741824, min_rows_for_wide_part = 0";

struct Config {
    std::string host = "127.0.0.1";
    std::int64_t port = 8123;
    std::string database = "default";
    std::string username = "default";
    std::string password;
    bool secure = false;
};

struct Result {
    std::int64_t contracts = 0;
    std::int64_t baskets = 0;
    std::vector<std::string> tables;
};

bool is_int_column(const std::string& column);
jsonio::Json cell(const std::string& column, const std::string& value);
std::string contracts_table(const std::string& date_dir);
std::string baskets_table(const std::string& date_dir);

class Client {
public:
    explicit Client(Config cfg);
    std::string command(const std::string& sql, const std::map<std::string, std::string>& settings = {},
                        bool use_database = true);
    void insert_json(const std::string& table, const std::vector<std::string>& columns, const std::string& body);
    const Config& config() const { return cfg_; }

private:
    Config cfg_;
};

Result push(const Config& cfg, const std::string& date_dir, bool update_current);

}  // namespace premarket::clickhouse
