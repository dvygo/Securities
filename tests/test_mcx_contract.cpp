#include "test_mcx_contract.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/mcx_contract.hpp"

using namespace premarket;

namespace {
session::Ymd ymd(const std::string& iso) {
    return {std::stoi(iso.substr(0, 4)), std::stoi(iso.substr(5, 2)), std::stoi(iso.substr(8, 2))};
}
}  // namespace

TEST_CASE("MCX rows map as 7.0.0 mapped them") {
    fixture::Tree tree;
    for (const auto& c : fixture::golden("mcx")) {
        auto raw = c["raw"].get<std::vector<std::string>>();
        REQUIRE(raw.size() == mcx_contract::WIDTH);
        CAPTURE(raw[mcx_contract::COL_SYMBOL]);
        fixture::same_leading_columns(mcx_contract::map_row(raw, {2026, 11, 3}), c["out"], row::def_first);
    }
}

TEST_CASE("metals and energy follow US daylight saving") {
    for (const auto& c : fixture::golden("mcx_dst")) {
        auto day = ymd(c["date"].get<std::string>());
        CAPTURE(c["date"].get<std::string>());
        CHECK(mcx_contract::us_on_standard_time(day) == c["std"].get<bool>());
        CHECK(mcx_contract::session_utc(mcx_contract::Profile::MetalsEnergy, day) == c["session"].get<std::string>());
    }
}

TEST_CASE("MCX profiles and strikes") {
    CHECK(mcx_contract::profile_for("PRECIOUS_METAL", "GOLD") == mcx_contract::Profile::MetalsEnergy);
    CHECK(mcx_contract::profile_for("UNKNOWNGRP", "X") == mcx_contract::Profile::None);
    CHECK(mcx_contract::strike_text(16600000) == "166000");
    CHECK(mcx_contract::strike_text(1662550) == "16625.5");
}
