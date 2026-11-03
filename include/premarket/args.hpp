#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace premarket::args {

struct Option {
    std::string name;
    bool takes_value = true;
    bool repeatable = false;
    std::string metavar = "VALUE";
    std::string help;
};

struct Parsed {
    std::map<std::string, std::vector<std::string>> values;
    std::map<std::string, bool> flags;
    std::vector<std::string> positionals;

    bool flag(const std::string& name) const;
    std::optional<std::string> value(const std::string& name) const;
    std::vector<std::string> all(const std::string& name) const;
};

struct UsageError {
    std::string message;
};

Parsed parse(const std::vector<std::string>& argv, const std::vector<Option>& options, std::size_t max_positionals = 0);
std::string describe(const std::vector<Option>& options);

}  // namespace premarket::args
