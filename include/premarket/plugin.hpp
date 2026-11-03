#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace premarket::plugin {

extern const std::vector<std::string> PLUGIN_COLUMNS;
inline constexpr std::size_t PLUGIN_CHUNK_ROWS = 50000;
inline constexpr std::int64_t UNDEF_PRICE = 9223372036854775807LL;

using Normalized = std::map<std::string, std::string>;
using PluginRow = std::vector<std::string>;

std::int64_t expiry_ns(const Normalized& row);
bool is_expired(const Normalized& row, std::int64_t cutoff_ns);
std::string ticksize(const Normalized& row);
std::string fullname(const std::string& inst_type2, const std::string& underlying, const std::string& strike,
                     const std::string& divisor, const std::string& opt_code, const std::string& expiry_str);
PluginRow map_row(const Normalized& row, const std::string& trade_date, const std::string& exchange);
std::vector<std::filesystem::path> build_day(const std::string& date_dir, const std::vector<std::string>& markets);

namespace tokenmap {

inline constexpr const char* MAGIC = "MDFVTOK1";
inline constexpr std::uint32_t FORMAT_VERSION = 1;
inline constexpr std::size_t HEADER_BYTES = 64;

struct Skips {
    std::int64_t unparseable_id = 0, unparseable_token = 0, reserved_id = 0, id_out_of_u32 = 0, token_out_of_range = 0;
    std::string describe() const;
};

struct Built {
    std::string venue;
    std::vector<std::pair<std::uint32_t, std::int32_t>> entries;
    std::uint64_t source_rows = 0;
    std::string key;
    Skips skips;
};

struct Info {
    std::string venue;
    std::uint32_t entries = 0, min_id = 0, max_id = 0;
    std::size_t bytes = 0;
};

std::string filename(const std::string& venue);
std::uint64_t fnv1a(const std::string& body);
std::string key_column(const std::vector<std::map<std::string, std::string>>& rows);
Built build(const std::string& venue, const std::vector<std::map<std::string, std::string>>& rows,
            const std::string& key = "");
std::string encode(const Built& built, std::optional<std::uint64_t> built_at_ns = std::nullopt);
Info verify(const std::string& blob, const std::string& venue = "");
std::filesystem::path tokenmap_dir(const std::string& date_dir);
std::vector<std::filesystem::path> emit(const std::string& date_dir, const std::vector<std::string>& markets,
                                        const std::filesystem::path& out_dir);

}  // namespace tokenmap
}  // namespace premarket::plugin
