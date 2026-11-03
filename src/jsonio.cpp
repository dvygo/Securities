#include "premarket/jsonio.hpp"

#include "premarket/util.hpp"

namespace premarket::jsonio {

std::string pretty(const Json& value) {
    return value.dump(1, ' ', false, Json::error_handler_t::replace) + "\n";
}

std::string line(const Json& value) { return value.dump(-1, ' ', false, Json::error_handler_t::replace); }

std::string line(const OrderedJson& value) { return value.dump(-1, ' ', false, OrderedJson::error_handler_t::replace); }

Json parse(const std::string& text) { return Json::parse(text); }

Json read(const std::filesystem::path& path) { return Json::parse(util::read_file(path)); }

}  // namespace premarket::jsonio
