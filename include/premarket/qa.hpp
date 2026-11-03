#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace premarket::qa {

inline constexpr const char* V2 = "v2";
inline constexpr const char* ALL = "ALL";

struct Check {
    std::string day;
    std::string venue;
    std::string name;
    bool ok = true;
    std::string detail;
    bool hard = true;
    std::string tag = ALL;

    std::string status() const;
    std::string line() const;
};

std::string tally(const std::vector<Check>& checks);
std::vector<std::filesystem::path> write_reports(const std::vector<Check>& checks, const std::string& suite);
int report(const std::vector<Check>& checks, const std::string& suite = "");

std::vector<Check> state_checks();
std::vector<Check> token_checks(const std::vector<std::string>& dates, const std::string& venue);
std::vector<Check> lineage_checks(const std::vector<std::string>& dates, const std::string& venue);

}  // namespace premarket::qa
