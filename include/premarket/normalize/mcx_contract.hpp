#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "premarket/normalize/row.hpp"
#include "premarket/normalize/session.hpp"
#include "premarket/util.hpp"
#include "premarket/runner.hpp"

namespace premarket::mcx_contract {

inline constexpr const char* MIC = "XIMC";
inline constexpr const char* FEED = "mcx";
inline constexpr const char* DROP_DIR = "MCX";
inline constexpr const char* MEMBER = "MCXScrips.bcp";
inline constexpr const char* OUTPUT = "XIMC-MCX.parquet";
inline constexpr std::size_t WIDTH = 118;
inline constexpr std::int64_t SCALE = 100;

inline constexpr std::size_t COL_FILE_TS = 1, COL_TOKEN = 5, COL_SYMBOL = 6, COL_EXPIRY = 19, COL_LOT = 20, COL_TICK = 21,
                             COL_DESC = 39, COL_GROUP = 43, COL_INST = 53, COL_STRIKE = 55, COL_OPTION = 56,
                             COL_PRODUCT_CODE = 95, COL_CURRENCY = 102;

struct DropError : ValueError {
    using ValueError::ValueError;
};

enum class Profile { None, MetalsEnergy, Agri, IntlAgri };

std::filesystem::path drop_dir(const std::string& as_of);
std::optional<std::filesystem::path> find_zip(const std::string& as_of);
std::vector<std::vector<std::string>> read_member(const std::filesystem::path& zip);
bool us_on_standard_time(const session::Ymd& day);
Profile profile_for(const std::string& group, const std::string& symbol);
std::string session_utc(Profile profile, const session::Ymd& day);
std::int64_t expiration_ns(std::int64_t expiry_unix, Profile profile);
std::string strike_text(std::int64_t paise);
std::string script_for(const std::vector<std::string>& row);
bool is_spread(const std::vector<std::string>& row);
row::Row map_row(const std::vector<std::string>& row, const session::Ymd& as_of_day);

struct Stats {
    std::size_t read = 0, malformed = 0, spreads = 0, expired = 0, collisions = 0;
    std::map<std::string, std::int64_t> unknown_type, unknown_group;
    std::optional<std::string> file_date;
};
std::pair<std::vector<row::Row>, Stats> rows_for(const std::string& as_of, const std::filesystem::path& zip);
void run(runner::Opts& opts);

}  // namespace premarket::mcx_contract
