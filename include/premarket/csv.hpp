#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace premarket::csv {

std::vector<std::string> parse_line(std::string_view line);
std::string format_line(const std::vector<std::string>& fields);

}  // namespace premarket::csv
