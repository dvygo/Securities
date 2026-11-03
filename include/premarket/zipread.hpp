#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace premarket::zipread {

std::vector<std::string> names(const std::filesystem::path& zip);
std::string read(const std::filesystem::path& zip, const std::string& name);

}  // namespace premarket::zipread
