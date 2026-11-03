#include "test_nse_contract.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/nse_contract.hpp"

using namespace premarket;

TEST_CASE("NSE cash rows map as 7.0.0 mapped them") {
    fixture::Tree tree;
    std::vector<nse_contract::Raw> rows;
    for (const auto& c : fixture::golden("nse_cash")) rows.push_back(fixture::strings(c["raw"]));
    auto by_isin = nse_contract::types_by_isin(rows);
    std::size_t i = 0;
    for (const auto& c : fixture::golden("nse_cash")) {
        CAPTURE(i);
        auto out = nse_contract::map_cash_row(rows[i++], &by_isin);
        if (c["out"].is_null()) {
            CHECK_FALSE(out);
            continue;
        }
        REQUIRE(out);
        fixture::same_leading_columns(*out, c["out"], row::def_first);
    }
}

TEST_CASE("NSE derivative rows map as 7.0.0 mapped them") {
    fixture::Tree tree;
    for (const auto& c : fixture::golden("nse_deriv")) {
        CAPTURE(c["raw"].dump());
        auto segment = c["segment"] == "CURRENCY" ? nse_contract::Segment::Currency : nse_contract::Segment::Deriv;
        auto out = nse_contract::map_derivative_row(fixture::strings(c["raw"]), segment);
        if (c["out"].is_null()) {
            CHECK_FALSE(out);
            continue;
        }
        REQUIRE(out);
        fixture::same_leading_columns(*out, c["out"], row::def_first);
    }
}

// 0 means "no date" in NSE's files and stays 0; everything else is shifted from 1980.
TEST_CASE("NSE seconds are counted from 1980") {
    CHECK(nse_contract::to_unix_seconds("0") == 0);
    CHECK(nse_contract::to_unix_seconds("1479477000") == 1795009800);
}
