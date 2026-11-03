#include "test_databento_source.hpp"

#include <doctest/doctest.h>

#include <cstring>

#include "fixture.hpp"
#include "premarket/normalize/databento_norm.hpp"
#include "premarket/sources/databento.hpp"

using namespace premarket;

namespace {

databento::InstrumentDefMsg def(std::uint32_t id, const char* symbol, std::uint64_t expiration, std::int64_t ts = 1) {
    databento::InstrumentDefMsg r{};
    r.hd.length = sizeof(databento::InstrumentDefMsg) / 4;
    r.hd.rtype = databento::RType::InstrumentDef;
    r.hd.instrument_id = id;
    r.hd.ts_event = databento::UnixNanos{std::chrono::nanoseconds{ts}};
    r.ts_recv = databento::UnixNanos{std::chrono::nanoseconds{ts}};
    r.expiration = databento::UnixNanos{std::chrono::nanoseconds{static_cast<std::int64_t>(expiration)}};
    std::strncpy(r.raw_symbol.data(), symbol, r.raw_symbol.size() - 1);
    return r;
}

std::int64_t ns(int y, int m, int d, int hh = 0) { return util::utc_epoch_seconds(y, m, d, hh) * 1000000000LL; }

}  // namespace

TEST_CASE("symbols files") {
    fixture::Tree tree;
    auto file = tree.root / "XCBO.txt";
    util::write_file(file, "SPY\n\n  SPX  \nES.FUT\n.SPX\nSPXW.OPT\n");
    config::Venue parent;
    parent.stype_in = "parent";
    parent.symbols = file;
    CHECK(databento_src::resolve_symbols(parent, std::nullopt) ==
          std::vector<std::string>{"SPY.OPT", "SPX.OPT", "ES.FUT", ".SPX", "SPXW.OPT"});
    config::Venue raw;
    raw.stype_in = "raw_symbol";
    raw.symbols = file;
    CHECK(databento_src::resolve_symbols(raw, std::nullopt).front() == "SPY");
    CHECK(databento_src::resolve_symbols(config::Venue{}, std::nullopt) == std::vector<std::string>{databento_src::ALL_SYMBOLS});
    CHECK_THROWS_AS(databento_src::resolve_symbols(raw, (tree.root / "gone.txt").string()), FileNotFoundError);
    CHECK(databento_src::dataset_key("OPRA.PILLAR") == "OPRA");
    CHECK(databento_src::dataset_key(" equs.mini") == "EQUS");
}

TEST_CASE("the last copy of each instrument wins, and expired ones are dropped") {
    auto day = ns(2026, 11, 3);
    std::vector<databento::InstrumentDefMsg> recs = {
        def(1, "OLD", static_cast<std::uint64_t>(day) - 1),
        def(2, "SPY", databento_src::UNDEF_TIMESTAMP, 1),
        def(3, "ESZ6", static_cast<std::uint64_t>(ns(2026, 12, 18)), 1),
        def(3, "ESZ6", static_cast<std::uint64_t>(ns(2026, 12, 19)), 2),
        def(4, "TODAY", static_cast<std::uint64_t>(day)),
    };
    auto sel = databento_src::select_live_definitions(recs, day);
    CHECK(sel.distinct == 4);
    REQUIRE(sel.kept.size() == 3);
    CHECK(sel.kept[0].hd.instrument_id == 2);
    CHECK(sel.kept[1].hd.instrument_id == 3);
    CHECK(sel.kept[1].expiration.time_since_epoch().count() == ns(2026, 12, 19));
    CHECK(sel.kept[2].hd.instrument_id == 4);
}

TEST_CASE("a written definition file reads back through normalize's reader") {
    fixture::Tree tree;
    std::vector<databento::InstrumentDefMsg> recs = {def(11, "ESZ6", static_cast<std::uint64_t>(ns(2026, 12, 18))),
                                                     def(12, "ESZ6 C6500", static_cast<std::uint64_t>(ns(2026, 12, 18)))};
    auto blob = databento_src::encode_definition_file(recs, "GLBX.MDP3", "parent", {"ES.FUT"}, ns(2026, 11, 3), ns(2026, 11, 3, 1));
    auto file = tree.root / "glbx-mdp3-20261103.definition.dbn.zst";
    util::write_file(file, blob);
    std::vector<std::pair<std::uint32_t, std::string>> back;
    databento_norm::for_each_definition({file}, "GLBX.MDP3", [&](const databento::InstrumentDefMsg& d) {
        back.emplace_back(d.hd.instrument_id, std::string(d.raw_symbol.data()));
    });
    CHECK(back == std::vector<std::pair<std::uint32_t, std::string>>{{11, "ESZ6"}, {12, "ESZ6 C6500"}});
}

TEST_CASE("OPRA's new listings follow New York time") {
    CHECK(databento_src::new_listings_utc(ns(2026, 8, 26, 10)) == "12:00");
    CHECK(databento_src::new_listings_utc(ns(2026, 11, 3, 10)) == "13:00");
    CHECK(databento_src::new_listings_utc(ns(2026, 3, 6, 10)) == "13:00");
    CHECK(databento_src::new_listings_utc(ns(2026, 3, 9, 10)) == "12:00");
    CHECK(databento_src::new_listings_utc(ns(2026, 10, 30, 10)) == "12:00");
    CHECK(databento_src::new_listings_utc(ns(2026, 11, 2, 10)) == "13:00");
}
