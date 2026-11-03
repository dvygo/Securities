#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace premarket {

namespace fs = std::filesystem;

struct ValueError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct RuntimeError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct FileNotFoundError : RuntimeError {
    using RuntimeError::RuntimeError;
};

struct SystemExit {
    int code = 1;
    std::string message;
};

namespace util {

std::string strip(std::string_view s);
std::string lstrip(std::string_view s);
std::string rstrip(std::string_view s);
std::string strip_chars(std::string_view s, std::string_view chars);
std::string rstrip_chars(std::string_view s, std::string_view chars);
std::string lower(std::string_view s);
std::string upper(std::string_view s);
bool starts_with(std::string_view s, std::string_view prefix);
bool ends_with(std::string_view s, std::string_view suffix);
bool contains(std::string_view s, std::string_view needle);
std::vector<std::string> split(std::string_view s, std::string_view sep);
std::vector<std::string> split_ws(std::string_view s);
std::vector<std::string> splitlines(std::string_view s);
std::string join(const std::vector<std::string>& parts, std::string_view sep);
std::string replace_all(std::string s, std::string_view from, std::string_view to);
bool is_digits(std::string_view s);

std::optional<std::int64_t> parse_int(std::string_view s);
std::optional<double> parse_float(std::string_view s);
std::optional<std::int64_t> parse_int_truncating(std::string_view s);
std::int64_t round_half_even(double x);
std::string commas(std::int64_t n);
std::string fixed(double x, int digits);
std::string zero_pad(std::int64_t n, int width);
std::string quoted(std::string_view s);
std::string utf8_replace_invalid(std::string_view s);
std::string latin1_to_utf8(std::string_view s);

std::string read_file(const fs::path& path);
std::string read_text_utf8_sig(const fs::path& path);
void write_file(const fs::path& path, std::string_view data);
void write_file_fsync(const fs::path& path, std::string_view data);
void fsync_dir(const fs::path& dir);
void replace_file(const fs::path& from, const fs::path& to);
fs::path staging_path(const fs::path& path);
std::vector<fs::path> list_dir_sorted(const fs::path& dir);
std::string sha256_file(const fs::path& path);
std::string sha256_bytes(std::string_view data);
std::string relative_to(const fs::path& path, const fs::path& base);

int pid();
std::string hostname();
std::string username();
std::string env(const char* name);

std::string utc_now_iso();
std::string utc_stamp_micro();
std::string local_today_yyyymmdd();
std::string local_now(const char* format);
std::int64_t unix_now_ns();

bool valid_yyyymmdd(std::string_view s);
std::string add_days(std::string_view yyyymmdd, int days);
int days_between(std::string_view earlier, std::string_view later);
std::int64_t day_start_ns(std::string_view yyyymmdd);
int utc_year_of_ns(std::int64_t ns);
std::string utc_format_ns(std::int64_t ns, const char* format);
std::int64_t utc_epoch_seconds(int year, int month, int day, int hour = 0, int minute = 0, int second = 0);

}  // namespace util
}  // namespace premarket
