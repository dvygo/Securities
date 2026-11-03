#include "premarket/util.hpp"

#include <date/date.h>
#include <fcntl.h>
#include <openssl/evp.h>
#include <pwd.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <system_error>

namespace premarket::util {

namespace {

constexpr std::string_view kWhitespace = " \t\n\r\x0b\x0c";

bool is_ws(char c) { return kWhitespace.find(c) != std::string_view::npos; }

std::string os_error(const std::string& what, const fs::path& path) {
    return what + " " + path.string() + ": " + std::strerror(errno);
}

}  // namespace

std::string strip(std::string_view s) { return lstrip(rstrip(s)); }

std::string lstrip(std::string_view s) {
    std::size_t i = 0;
    while (i < s.size() && is_ws(s[i])) ++i;
    return std::string(s.substr(i));
}

std::string rstrip(std::string_view s) {
    std::size_t n = s.size();
    while (n > 0 && is_ws(s[n - 1])) --n;
    return std::string(s.substr(0, n));
}

std::string strip_chars(std::string_view s, std::string_view chars) {
    std::size_t b = 0, e = s.size();
    while (b < e && chars.find(s[b]) != std::string_view::npos) ++b;
    while (e > b && chars.find(s[e - 1]) != std::string_view::npos) --e;
    return std::string(s.substr(b, e - b));
}

std::string rstrip_chars(std::string_view s, std::string_view chars) {
    std::size_t e = s.size();
    while (e > 0 && chars.find(s[e - 1]) != std::string_view::npos) --e;
    return std::string(s.substr(0, e));
}

std::string lower(std::string_view s) {
    std::string out(s);
    for (auto& c : out)
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    return out;
}

std::string upper(std::string_view s) {
    std::string out(s);
    for (auto& c : out)
        if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
    return out;
}

bool starts_with(std::string_view s, std::string_view prefix) { return s.starts_with(prefix); }
bool ends_with(std::string_view s, std::string_view suffix) { return s.ends_with(suffix); }
bool contains(std::string_view s, std::string_view needle) {
    return s.find(needle) != std::string_view::npos;
}

std::vector<std::string> split(std::string_view s, std::string_view sep) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (true) {
        auto pos = s.find(sep, start);
        if (pos == std::string_view::npos) {
            out.emplace_back(s.substr(start));
            return out;
        }
        out.emplace_back(s.substr(start, pos - start));
        start = pos + sep.size();
    }
}

std::vector<std::string> split_ws(std::string_view s) {
    std::vector<std::string> out;
    std::size_t i = 0;
    while (i < s.size()) {
        while (i < s.size() && is_ws(s[i])) ++i;
        if (i >= s.size()) break;
        std::size_t j = i;
        while (j < s.size() && !is_ws(s[j])) ++j;
        out.emplace_back(s.substr(i, j - i));
        i = j;
    }
    return out;
}

std::vector<std::string> splitlines(std::string_view s) {
    std::vector<std::string> out;
    std::size_t i = 0, start = 0;
    while (i < s.size()) {
        char c = s[i];
        if (c == '\n' || c == '\r' || c == '\x0b' || c == '\x0c' || c == '\x1c' || c == '\x1d' ||
            c == '\x1e') {
            out.emplace_back(s.substr(start, i - start));
            if (c == '\r' && i + 1 < s.size() && s[i + 1] == '\n') ++i;
            start = i + 1;
        }
        ++i;
    }
    if (start < s.size()) out.emplace_back(s.substr(start));
    return out;
}

std::string join(const std::vector<std::string>& parts, std::string_view sep) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i) out.append(sep);
        out.append(parts[i]);
    }
    return out;
}

std::string replace_all(std::string s, std::string_view from, std::string_view to) {
    if (from.empty()) return s;
    std::size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
    return s;
}

bool is_digits(std::string_view s) {
    return !s.empty() && std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; });
}

namespace {

std::optional<std::string> without_underscores(std::string_view body, bool allow_point) {
    std::string out;
    out.reserve(body.size());
    for (std::size_t i = 0; i < body.size(); ++i) {
        char c = body[i];
        if (c == '_') {
            auto digit = [](char d) { return d >= '0' && d <= '9'; };
            if (i == 0 || i + 1 >= body.size() || !digit(body[i - 1]) || !digit(body[i + 1]))
                return std::nullopt;
            continue;
        }
        if (!allow_point && !(c >= '0' && c <= '9')) return std::nullopt;
        out.push_back(c);
    }
    return out;
}

}  // namespace

std::optional<std::int64_t> parse_int(std::string_view s) {
    std::string t = strip(s);
    if (t.empty()) return std::nullopt;
    bool negative = false;
    std::string_view body = t;
    if (body[0] == '+' || body[0] == '-') {
        negative = body[0] == '-';
        body.remove_prefix(1);
    }
    if (body.empty()) return std::nullopt;
    auto digits = without_underscores(body, false);
    if (!digits || digits->empty()) return std::nullopt;
    std::uint64_t value = 0;
    auto [ptr, ec] = std::from_chars(digits->data(), digits->data() + digits->size(), value);
    if (ec != std::errc() || ptr != digits->data() + digits->size()) return std::nullopt;
    if (negative) {
        if (value > static_cast<std::uint64_t>(INT64_MAX) + 1) return std::nullopt;
        return static_cast<std::int64_t>(0 - value);
    }
    if (value > static_cast<std::uint64_t>(INT64_MAX)) return std::nullopt;
    return static_cast<std::int64_t>(value);
}

std::optional<double> parse_float(std::string_view s) {
    std::string t = strip(s);
    if (t.empty()) return std::nullopt;
    bool negative = false;
    std::string_view body = t;
    if (body[0] == '+' || body[0] == '-') {
        negative = body[0] == '-';
        body.remove_prefix(1);
    }
    if (body.empty()) return std::nullopt;
    std::string low = lower(body);
    if (low == "inf" || low == "infinity") return negative ? -HUGE_VAL : HUGE_VAL;
    if (low == "nan") return negative ? -NAN : NAN;
    for (char c : body) {
        bool ok = (c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' ||
                  c == '-' || c == '_';
        if (!ok) return std::nullopt;
    }
    std::string clean;
    for (std::size_t i = 0; i < body.size(); ++i) {
        char c = body[i];
        if (c == '_') {
            auto digit = [](char d) { return d >= '0' && d <= '9'; };
            if (i == 0 || i + 1 >= body.size() || !digit(body[i - 1]) || !digit(body[i + 1]))
                return std::nullopt;
            continue;
        }
        clean.push_back(c);
    }
    if (clean.empty() || clean[0] == '+' || clean[0] == '-') return std::nullopt;
    char* end = nullptr;
    errno = 0;
    double value = std::strtod(clean.c_str(), &end);
    if (end != clean.c_str() + clean.size()) return std::nullopt;
    return negative ? -value : value;
}

std::optional<std::int64_t> parse_int_truncating(std::string_view s) {
    auto f = parse_float(s);
    if (!f || std::isnan(*f) || std::isinf(*f)) return std::nullopt;
    double t = std::trunc(*f);
    if (t >= 9.2233720368547758e18 || t < -9.2233720368547758e18) return std::nullopt;
    return static_cast<std::int64_t>(t);
}

std::int64_t round_half_even(double x) { return static_cast<std::int64_t>(std::nearbyint(x)); }

std::string commas(std::int64_t n) {
    unsigned long long magnitude = n < 0 ? 0ULL - static_cast<unsigned long long>(n)
                                         : static_cast<unsigned long long>(n);
    std::string digits = std::to_string(magnitude);
    std::string out;
    int count = 0;
    for (auto it = digits.rbegin(); it != digits.rend(); ++it) {
        if (count && count % 3 == 0) out.push_back(',');
        out.push_back(*it);
        ++count;
    }
    if (n < 0) out.push_back('-');
    std::reverse(out.begin(), out.end());
    return out;
}

std::string fixed(double x, int digits) {
    std::array<char, 512> buf{};
    std::snprintf(buf.data(), buf.size(), "%.*f", digits, x);
    return buf.data();
}

std::string zero_pad(std::int64_t n, int width) {
    std::string s = std::to_string(n < 0 ? -n : n);
    if (static_cast<int>(s.size()) < width) s.insert(0, static_cast<std::size_t>(width) - s.size(), '0');
    return n < 0 ? "-" + s : s;
}

std::string quoted(std::string_view s) {
    bool has_single = s.find('\'') != std::string_view::npos;
    bool has_double = s.find('"') != std::string_view::npos;
    char q = has_single && !has_double ? '"' : '\'';
    std::string out(1, q);
    for (unsigned char c : s) {
        if (c == '\\') out += "\\\\";
        else if (c == static_cast<unsigned char>(q)) { out += '\\'; out += static_cast<char>(c); }
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c < 0x20 || c == 0x7f) {
            std::array<char, 8> b{};
            std::snprintf(b.data(), b.size(), "\\x%02x", c);
            out += b.data();
        } else out += static_cast<char>(c);
    }
    out += q;
    return out;
}

std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        if (!fs::exists(path)) throw FileNotFoundError("No such file or directory: '" + path.string() + "'");
        throw RuntimeError(os_error("cannot read", path));
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

std::string read_text_utf8_sig(const fs::path& path) {
    std::string text = read_file(path);
    if (text.size() >= 3 && static_cast<unsigned char>(text[0]) == 0xEF &&
        static_cast<unsigned char>(text[1]) == 0xBB && static_cast<unsigned char>(text[2]) == 0xBF)
        text.erase(0, 3);
    return text;
}

void write_file(const fs::path& path, std::string_view data) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out) throw RuntimeError(os_error("cannot write", path));
    out.write(data.data(), static_cast<std::streamsize>(data.size()));
    if (!out) throw RuntimeError(os_error("cannot write", path));
}

void write_file_fsync(const fs::path& path, std::string_view data) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) throw RuntimeError(os_error("cannot write", path));
    std::size_t done = 0;
    while (done < data.size()) {
        auto n = ::write(fd, data.data() + done, data.size() - done);
        if (n < 0) {
            if (errno == EINTR) continue;
            ::close(fd);
            throw RuntimeError(os_error("cannot write", path));
        }
        done += static_cast<std::size_t>(n);
    }
    ::fsync(fd);
    ::close(fd);
}

void fsync_dir(const fs::path& dir) {
    int fd = ::open(dir.c_str(), O_RDONLY);
    if (fd < 0) return;
    ::fsync(fd);
    ::close(fd);
}

void replace_file(const fs::path& from, const fs::path& to) {
    if (std::rename(from.c_str(), to.c_str()) != 0) throw RuntimeError(os_error("cannot replace", to));
}

fs::path staging_path(const fs::path& path) {
    return path.parent_path() / (path.filename().string() + ".tmp." + std::to_string(pid()));
}

std::vector<fs::path> list_dir_sorted(const fs::path& dir) {
    std::vector<fs::path> out;
    std::error_code ec;
    for (auto it = fs::directory_iterator(dir, ec); !ec && it != fs::directory_iterator(); it.increment(ec))
        out.push_back(it->path());
    std::sort(out.begin(), out.end(),
              [](const fs::path& a, const fs::path& b) { return a.filename().string() < b.filename().string(); });
    return out;
}

namespace {

std::string hex(const unsigned char* data, unsigned int n) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    out.reserve(n * 2);
    for (unsigned int i = 0; i < n; ++i) {
        out.push_back(digits[data[i] >> 4]);
        out.push_back(digits[data[i] & 0xF]);
    }
    return out;
}

}  // namespace

std::string sha256_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw RuntimeError(os_error("cannot read", path));
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr);
    std::vector<char> block(1 << 20);
    while (in) {
        in.read(block.data(), static_cast<std::streamsize>(block.size()));
        auto got = in.gcount();
        if (got > 0) EVP_DigestUpdate(ctx, block.data(), static_cast<std::size_t>(got));
    }
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned int len = 0;
    EVP_DigestFinal_ex(ctx, md, &len);
    EVP_MD_CTX_free(ctx);
    return hex(md, len);
}

std::string sha256_bytes(std::string_view data) {
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned int len = 0;
    EVP_Digest(data.data(), data.size(), md, &len, EVP_sha256(), nullptr);
    return hex(md, len);
}

std::string relative_to(const fs::path& path, const fs::path& base) {
    auto p = path.lexically_normal().string();
    auto b = base.lexically_normal().string();
    if (!b.empty() && b.back() != '/') b.push_back('/');
    if (p.rfind(b, 0) != 0) throw ValueError(path.string() + " is not in the subpath of " + base.string());
    return p.substr(b.size());
}

int pid() { return static_cast<int>(::getpid()); }

std::string hostname() {
    std::array<char, 256> buf{};
    if (::gethostname(buf.data(), buf.size() - 1) != 0) return "localhost";
    return buf.data();
}

std::string username() {
    for (const char* name : {"LOGNAME", "USER", "LNAME", "USERNAME"}) {
        const char* v = std::getenv(name);
        if (v && *v) return v;
    }
    if (auto* pw = ::getpwuid(::getuid()); pw && pw->pw_name) return pw->pw_name;
    return "unknown";
}

std::string env(const char* name) {
    const char* v = std::getenv(name);
    return v ? std::string(v) : std::string();
}

namespace {

std::string strftime_tm(const std::tm& tm, const char* format) {
    std::array<char, 128> buf{};
    auto n = std::strftime(buf.data(), buf.size(), format, &tm);
    return std::string(buf.data(), n);
}

}  // namespace

std::string utc_now_iso() {
    std::time_t t = std::time(nullptr);
    std::tm tm{};
    ::gmtime_r(&t, &tm);
    return strftime_tm(tm, "%Y-%m-%dT%H:%M:%SZ");
}

std::string utc_stamp_micro() {
    auto now = std::chrono::system_clock::now();
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();
    std::time_t t = static_cast<std::time_t>(us / 1000000);
    std::tm tm{};
    ::gmtime_r(&t, &tm);
    return strftime_tm(tm, "%Y%m%dT%H%M%S") + zero_pad(us % 1000000, 6) + "Z";
}

std::string local_today_yyyymmdd() { return local_now("%Y%m%d"); }

std::string local_now(const char* format) {
    std::time_t t = std::time(nullptr);
    std::tm tm{};
    ::localtime_r(&t, &tm);
    return strftime_tm(tm, format);
}

std::int64_t unix_now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

namespace {

std::optional<date::year_month_day> parse_ymd(std::string_view s) {
    if (s.size() != 8 || !is_digits(s)) return std::nullopt;
    int y = std::stoi(std::string(s.substr(0, 4)));
    unsigned m = static_cast<unsigned>(std::stoi(std::string(s.substr(4, 2))));
    unsigned d = static_cast<unsigned>(std::stoi(std::string(s.substr(6, 2))));
    date::year_month_day ymd{date::year{y}, date::month{m}, date::day{d}};
    if (!ymd.ok() || y < 1) return std::nullopt;
    return ymd;
}

std::string format_ymd(const date::year_month_day& ymd) {
    return zero_pad(static_cast<int>(ymd.year()), 4) + zero_pad(static_cast<unsigned>(ymd.month()), 2) +
           zero_pad(static_cast<unsigned>(ymd.day()), 2);
}

}  // namespace

bool valid_yyyymmdd(std::string_view s) { return parse_ymd(s).has_value(); }

std::string add_days(std::string_view yyyymmdd, int days) {
    auto ymd = parse_ymd(yyyymmdd);
    if (!ymd) throw ValueError("time data '" + std::string(yyyymmdd) + "' does not match format '%Y%m%d'");
    return format_ymd(date::year_month_day{date::sys_days{*ymd} + date::days{days}});
}

int days_between(std::string_view earlier, std::string_view later) {
    auto a = parse_ymd(earlier), b = parse_ymd(later);
    if (!a || !b) throw ValueError("time data does not match format '%Y%m%d'");
    return static_cast<int>((date::sys_days{*b} - date::sys_days{*a}).count());
}

std::int64_t day_start_ns(std::string_view yyyymmdd) {
    auto ymd = parse_ymd(yyyymmdd);
    if (!ymd) throw ValueError("time data '" + std::string(yyyymmdd) + "' does not match format '%Y%m%d'");
    auto secs = std::chrono::duration_cast<std::chrono::seconds>(date::sys_days{*ymd}.time_since_epoch()).count();
    return static_cast<std::int64_t>(secs) * 1000000000LL;
}

int utc_year_of_ns(std::int64_t ns) {
    double seconds = static_cast<double>(ns) / 1e9;
    auto whole = static_cast<std::int64_t>(std::floor(seconds));
    auto days = date::floor<date::days>(date::sys_seconds{std::chrono::seconds{whole}});
    return static_cast<int>(date::year_month_day{days}.year());
}

std::string utc_format_ns(std::int64_t ns, const char* format) {
    double seconds = static_cast<double>(ns) / 1e9;
    std::time_t t = static_cast<std::time_t>(std::floor(seconds));
    std::tm tm{};
    ::gmtime_r(&t, &tm);
    return strftime_tm(tm, format);
}

std::int64_t utc_epoch_seconds(int year, int month, int day, int hour, int minute, int second) {
    date::year_month_day ymd{date::year{year}, date::month{static_cast<unsigned>(month)},
                             date::day{static_cast<unsigned>(day)}};
    auto tp = date::sys_days{ymd} + std::chrono::hours{hour} + std::chrono::minutes{minute} +
              std::chrono::seconds{second};
    return std::chrono::duration_cast<std::chrono::seconds>(tp.time_since_epoch()).count();
}

}  // namespace premarket::util
