#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "premarket/normalize/row.hpp"
#include "premarket/runner.hpp"

namespace premarket::nse_contract {

using Raw = std::map<std::string, std::string>;

inline constexpr const char* DROP_DIR = "NEW FILE FORMAT";
inline constexpr std::int64_t NSE_EPOCH_OFFSET = 315532800;
inline constexpr const char* CM_FILE = "NSE_CM_security.csv";
inline constexpr const char* FO_FILE = "NSE_FO_contract.csv";
inline constexpr const char* CD_FILE = "NSE_CD_contract.csv";
inline constexpr const char* OUTPUT = "XNSE-NSE.parquet";

enum class Segment { Cash, Deriv, Currency };

std::filesystem::path drop_dir(const std::string& as_of);
bool present(const std::string& as_of);
std::int64_t to_unix_seconds(const std::string& nse_seconds);
std::int64_t expiration_ns(const std::string& nse_seconds, Segment segment);
std::string cash_instrument_type(const Raw& row, const std::map<std::string, std::string>* by_isin = nullptr);
std::map<std::string, std::string> types_by_isin(const std::vector<Raw>& rows);
std::optional<row::Row> map_cash_row(const Raw& row, const std::map<std::string, std::string>* by_isin = nullptr);
std::optional<row::Row> map_derivative_row(const Raw& row, Segment segment);
std::vector<Raw> read_csv(const std::filesystem::path& path);
std::pair<std::vector<row::Row>, std::vector<std::pair<std::string, std::size_t>>> rows_for(const std::string& as_of);
void run(runner::Opts& opts);

}  // namespace premarket::nse_contract
