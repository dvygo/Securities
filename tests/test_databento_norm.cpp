#include "test_databento_norm.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/jsonio.hpp"
#include "premarket/normalize/databento_norm.hpp"
#include "premarket/paths.hpp"

using namespace premarket;

// probe.dbn holds three hand-built records (an outright future with unset prices, a
// two-leg spread, an OPRA call) and probe_expected.json how 7.0.0 rendered every field of
// each. The renderer must agree on all 70 fields of all three.
TEST_CASE("definition fields render exactly as 7.x rendered them") {
    fixture::Tree tree;
    std::string dir = std::string(PREMARKET_SOURCE_DIR) + "/tests/data";
    auto expected = jsonio::read(dir + "/probe_expected.json");
    std::vector<databento_norm::DefRow> rows;
    databento_norm::for_each_definition({dir + "/probe.dbn"}, "GLBX.MDP3",
                                        [&](const databento::InstrumentDefMsg& d) { rows.push_back(databento_norm::def_row(d)); });
    REQUIRE(rows.size() == expected.size());
    for (std::size_t i = 0; i < rows.size(); ++i) {
        CAPTURE(i);
        for (std::size_t f = 0; f < paths::DEFINITION_FIELDS.size(); ++f) {
            CAPTURE(paths::DEFINITION_FIELDS[f]);
            CHECK(rows[i].fields[f] == expected[i][paths::DEFINITION_FIELDS[f]].get<std::string>());
        }
        CHECK(rows[i].instrument_class == expected[i]["_instrument_class"].get<std::string>());
        CHECK(rows[i].raw_symbol == expected[i]["_raw_symbol"].get<std::string>());
    }
}

TEST_CASE("a file for another dataset is skipped, not mixed in") {
    fixture::Tree tree;
    std::string dir = std::string(PREMARKET_SOURCE_DIR) + "/tests/data";
    int n = 0;
    databento_norm::for_each_definition({dir + "/probe.dbn"}, "OPRA.PILLAR", [&](const auto&) { ++n; }, false);
    CHECK(n == 0);
}

TEST_CASE("OCC symbols") {
    auto occ = databento_norm::parse_occ_symbol("NVDA  270115P00090000");
    REQUIRE(occ);
    CHECK(occ->underlying == "NVDA");
    CHECK(occ->expiration == "20270115");
    CHECK(occ->option_type == "PUT");
    CHECK(occ->strike == 90.0);
    // 2000 + YY for every year: SPX LEAPS reaching 2031 once came out as 1931 under a pivot.
    CHECK(databento_norm::parse_occ_symbol("SPX   311219C05000000")->expiration == "20311219");
    CHECK_FALSE(databento_norm::parse_occ_symbol("QQQ"));
}

TEST_CASE("GLBX contract month") {
    session::Ymd ref{2026, 8, 26};
    // Decade from the venue's expiry when there is one: 0BZ0 expires 2031-01-01 and is Z30.
    std::int64_t jan2031 = util::utc_epoch_seconds(2031, 1, 1) * 1000000000LL;
    CHECK(databento_norm::glbx_expiration_ns("0BZ0", ref, jan2031) == util::utc_epoch_seconds(2030, 12, 1) * 1000000000LL);
    // Without one, the ten years from one year back: "1" in 2026 is 2031, not 2021.
    CHECK(databento_norm::glbx_expiration_ns("0NGF1", ref) == util::utc_epoch_seconds(2031, 1, 1) * 1000000000LL);
    CHECK(databento_norm::glbx_expiration_ns("ESZ6", ref) == util::utc_epoch_seconds(2026, 12, 1) * 1000000000LL);
    CHECK(databento_norm::glbx_expiration_ns("ES", ref) == 0);
}

TEST_CASE("XNAS underlying roots keep interior dots") {
    CHECK(databento_norm::underlying_root_from_stype_in("BRK.B") == "BRK.B");
    CHECK(databento_norm::underlying_root_from_stype_in(".SPX") == "SPX");
    CHECK(databento_norm::underlying_root_from_stype_in("AAPL") == "AAPL");
    CHECK(databento_norm::underlying_root_from_stype_in("123") == "123");
}
