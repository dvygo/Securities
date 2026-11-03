#include "premarket/normalize/broker_script.hpp"

#include <cmath>

#include "premarket/util.hpp"

namespace premarket::broker_script {

namespace {

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'; }
bool is_digit(char c) { return c >= '0' && c <= '9'; }

}  // namespace

bool is_month_code(char c) {
    switch (c) {
        case 'F': case 'G': case 'H': case 'J': case 'K': case 'M':
        case 'N': case 'Q': case 'U': case 'V': case 'X': case 'Z': return true;
        default: return false;
    }
}

std::optional<GlbxOption> match_glbx_option(const std::string& s) {
    std::size_t end = s.size();
    while (end > 0 && is_space(s[end - 1])) --end;
    std::size_t i = end;
    while (i > 0 && is_digit(s[i - 1])) --i;
    if (i == end) return std::nullopt;
    std::size_t number_start = i;
    if (i > 0 && s[i - 1] == '.') {
        std::size_t j = i - 1;
        std::size_t k = j;
        while (k > 0 && is_digit(s[k - 1])) --k;
        if (k == j) return std::nullopt;
        number_start = k;
    }
    if (number_start == 0) return std::nullopt;
    char cp = s[number_start - 1];
    if (cp != 'C' && cp != 'P') return std::nullopt;
    std::size_t ws_end = number_start - 1;
    std::size_t ws = ws_end;
    while (ws > 0 && is_space(s[ws - 1])) --ws;
    if (ws == ws_end) return std::nullopt;
    return GlbxOption{ws, cp, s.substr(number_start, end - number_start)};
}

std::optional<GlbxBase> match_glbx_base(const std::string& base) {
    if (base.size() < 3) return std::nullopt;
    char month = base[base.size() - 2];
    char year = base.back();
    if (!is_month_code(month) || !is_digit(year)) return std::nullopt;
    return GlbxBase{base.substr(0, base.size() - 2), month, year - '0'};
}

std::string format_strike(double value) {
    std::string s = util::fixed(value, 4);
    s = util::rstrip_chars(s, "0");
    s = util::rstrip_chars(s, ".");
    return s.empty() ? "0" : s;
}

std::string format_strike_text(const std::string& value) {
    auto f = util::parse_float(value);
    if (!f) return value;
    return format_strike(*f);
}

std::optional<std::string> year2_from_expiration_ns(std::int64_t expiration_ns) {
    if (expiration_ns <= 0) return std::nullopt;
    return util::zero_pad(util::utc_year_of_ns(expiration_ns) % 100, 2);
}

std::string from_equity(const std::string& script) { return script; }

std::string from_occ(const std::string& script, const std::optional<Occ>& parsed) {
    if (!parsed) return script;
    std::string root = util::upper(parsed->underlying);
    const std::string& yyyymmdd = parsed->expiration;
    const std::string& option_type = parsed->option_type;
    if (root.empty() || yyyymmdd.size() != 8 || (option_type != "CALL" && option_type != "PUT")) return script;
    char cp = option_type == "CALL" ? 'C' : 'P';
    return root + "/" + yyyymmdd.substr(2) + "/" + format_strike(parsed->strike) + cp;
}

std::string from_glbx(const std::string& script, std::int64_t expiration_ns) {
    std::string s = util::strip(script);
    if (s.empty()) return "";
    std::string base = s;
    std::optional<char> cp;
    std::string strike;
    if (auto opt = match_glbx_option(s)) {
        base = util::strip(s.substr(0, opt->start));
        cp = opt->cp;
        strike = opt->strike;
    }
    if (base.find_first_of("-: ") != std::string::npos) return script;
    auto b = match_glbx_base(base);
    if (!b) return script;
    auto year2 = year2_from_expiration_ns(expiration_ns);
    if (!year2) return script;
    std::string contract = b->root + "/" + b->month + *year2;
    return cp ? contract + "/" + format_strike_text(strike) + *cp : contract;
}

}  // namespace premarket::broker_script
