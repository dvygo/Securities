#pragma once

#include <filesystem>
#include <nlohmann/json.hpp>
#include <string>

namespace premarket::jsonio {

using Json = nlohmann::json;
using OrderedJson = nlohmann::ordered_json;

std::string pretty(const Json& value);
std::string line(const Json& value);
std::string line(const OrderedJson& value);
Json parse(const std::string& text);
Json read(const std::filesystem::path& path);

}  // namespace premarket::jsonio
