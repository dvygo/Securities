#include "test_fields.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/fields.hpp"

using namespace premarket;

TEST_CASE("the Fyers mapper agrees with 7.0.0") {
    fixture::Tree tree;
    const auto& cases = fixture::golden("fyers");
    REQUIRE(cases.size() == 304);
    for (const auto& c : cases) {
        CAPTURE(c["raw"]["symTicker"].get<std::string>());
        fyers::Row raw;
        for (const auto& [k, v] : fixture::strings(c["raw"])) raw[k] = v;
        auto mapped = fields::map_fyers_row(raw);
        fixture::same_leading_columns(mapped.row, c["out"], row::def_first);
        CHECK(mapped.exchange == c["out"]["exchange"].get<std::string>());
    }
}

TEST_CASE("Fyers expiry and integer parsing") {
    CHECK(fields::expiration_ns("") == "0");
    CHECK(fields::safe_int("12") == 12);
    CHECK_FALSE(fields::safe_int("x"));
    CHECK(fields::instrument_type2("FUTIDX") == "FUTURE");
}
