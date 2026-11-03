#include "test_plugin.hpp"

#include <doctest/doctest.h>

#include <iomanip>
#include <sstream>

#include "fixture.hpp"
#include "premarket/plugin.hpp"

using namespace premarket;

namespace {
std::string hex(const std::string& bytes) {
    std::ostringstream out;
    for (unsigned char b : bytes) out << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(b);
    return out.str();
}
}  // namespace

TEST_CASE("plugin rows agree with 7.0.0") {
    fixture::Tree tree;
    auto cutoff = util::day_start_ns("20261103");
    for (const auto& c : fixture::golden("plugin")) {
        auto row = fixture::strings(c["row"]);
        CAPTURE(row["script"]);
        CHECK(plugin::is_expired(row, cutoff) == c["expired"].get<bool>());
        auto out = plugin::map_row(row, "2026-11-03", c["mic"].get<std::string>());
        REQUIRE(out.size() == plugin::PLUGIN_COLUMNS.size());
        for (std::size_t i = 0; i < out.size(); ++i) {
            CAPTURE(plugin::PLUGIN_COLUMNS[i]);
            CHECK(out[i] == c["out"][plugin::PLUGIN_COLUMNS[i]].get<std::string>());
        }
    }
}

TEST_CASE("the MDF token map is byte-identical to 7.0.0's") {
    const auto& g = fixture::golden("tokenmap");
    std::vector<std::map<std::string, std::string>> rows;
    for (const auto& r : g["rows"]) rows.push_back(fixture::strings(r));
    auto built = plugin::tokenmap::build("XNAS", rows);
    CHECK(built.key == g["key"].get<std::string>());
    CHECK(built.skips.describe() == g["skips"].get<std::string>());
    auto blob = plugin::tokenmap::encode(built, 1793000000000000000ULL);
    CHECK(hex(blob) == g["hex"].get<std::string>());
    auto info = plugin::tokenmap::verify(blob, "XNAS");
    CHECK(info.venue == "XNAS");
    CHECK(info.entries == built.entries.size());
    CHECK(info.bytes == blob.size());
    CHECK_THROWS(plugin::tokenmap::verify(blob, "XCME"));
    auto broken = blob;
    broken.back() = static_cast<char>(broken.back() ^ 1);
    CHECK_THROWS(plugin::tokenmap::verify(broken));
}
