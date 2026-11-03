#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace premarket::broker_script {

struct GlbxOption {
    std::size_t start;
    char cp;
    std::string strike;
};

struct GlbxBase {
    std::string root;
    char month;
    int year_digit;
};

struct Occ {
    std::string underlying;
    std::string expiration;
    std::string option_type;
    double strike = 0;
};

std::optional<GlbxOption> match_glbx_option(const std::string& s);
std::optional<GlbxBase> match_glbx_base(const std::string& base);
bool is_month_code(char c);

std::string format_strike(double value);
std::string format_strike_text(const std::string& value);
std::optional<std::string> year2_from_expiration_ns(std::int64_t expiration_ns);

std::string from_equity(const std::string& script);
std::string from_occ(const std::string& script, const std::optional<Occ>& parsed);
std::string from_glbx(const std::string& script, std::int64_t expiration_ns);

}  // namespace premarket::broker_script
