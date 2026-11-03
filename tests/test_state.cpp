#include "test_state.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/state.hpp"

using namespace premarket;

namespace {

numbering::Holdings holdings() {
    return numbering::Holdings{10, {{"B", 5}, {"A", 4}}, {9, 7}, {{"A", "20260925"}, {"B", "20260924"}}};
}

state::VenueHead venue_head(std::int64_t venue_id, const state::Snapshot& snap) {
    state::VenueHead vh;
    vh.venue_id = venue_id;
    vh.snapshot = snap.relative;
    vh.sha256 = snap.sha256;
    vh.rows = snap.rows;
    return vh;
}

}  // namespace

TEST_CASE("head: none on a fresh tree, and require_head names init-state") {
    fixture::Tree tree;
    CHECK_FALSE(state::read_head());
    CHECK_THROWS_WITH_AS(state::require_head(), doctest::Contains("init-state"), state::StateMissing);
}

TEST_CASE("head: it round-trips") {
    fixture::Tree tree;
    state::Head head;
    head.counter = 138744;
    head.last_run = {{"run_id", "r1"}};
    state::VenueHead vh;
    vh.venue_id = 16;
    vh.snapshot = "XNSE/r1.alloc.parquet";
    vh.sha256 = "ab";
    vh.rows = 3;
    vh.newest = "20260925";
    vh.newest_mode = "live";
    vh.live_date = "20260925";
    vh.run_id = "r1";
    head.venues["XNSE"] = vh;
    state::write_head(head);
    auto again = state::read_head();
    REQUIRE(again);
    CHECK(again->counter == 138744);
    CHECK(again->version == 8);
    CHECK(again->venues.at("XNSE").live_date == "20260925");
    CHECK(again->venues.at("XNSE").venue_id_int() == 16);
    CHECK(again->last_run == jsonio::Json{{"run_id", "r1"}});
    CHECK(jsonio::read(state::head_path())["version"] == 8);
}

TEST_CASE("head: unreadable, 7.x and future heads are refused, not started over") {
    fixture::Tree tree;
    fs::create_directories(state::state_dir());
    util::write_file(state::head_path(), "{ not json");
    CHECK_THROWS_WITH_AS(state::read_head(), doctest::Contains("Refusing"), state::StateCorrupt);
    util::write_file(state::head_path(), R"({"version": 1, "counter": 5, "venues": {}})");
    CHECK_THROWS_WITH_AS(state::read_head(), doctest::Contains("is a 7.x numbering state (head version 1)"),
                         state::StateCorrupt);
    CHECK_THROWS_WITH(state::read_head(), doctest::Contains("premarket migrate-state"));
    util::write_file(state::head_path(), R"({"version": 99})");
    CHECK_THROWS_WITH_AS(state::read_head(), doctest::Contains("version 99"), state::StateCorrupt);
}

TEST_CASE("head: it is written atomically") {
    fixture::Tree tree;
    state::Head head;
    head.counter = 1;
    state::write_head(head);
    CHECK(fixture::files_under(state::state_dir(), {}) == std::vector<std::string>{"manifest.json"});
}

TEST_CASE("snapshots: round trip, same digest, reuse") {
    fixture::Tree tree;
    auto snap = state::write_snapshot("xcme", "r1", holdings());
    CHECK(snap.relative == "XCME/r1.alloc.parquet");
    CHECK(snap.rows == 4);
    auto back = state::load_snapshot("XCME", venue_head(10, snap));
    CHECK(back.assigned == counter_token::TokenMap{{"A", 4}, {"B", 5}});
    CHECK(back.free == std::vector<std::int64_t>{7, 9});
    CHECK(back.last_date == std::map<std::string, std::string>{{"A", "20260925"}, {"B", "20260924"}});

    auto second = state::write_snapshot("XCME", "r2", holdings());
    CHECK(second.sha256 == snap.sha256);

    fixture::Tree fresh;
    auto first = state::write_snapshot("XCME", "r1", holdings());
    auto current = venue_head(10, first);
    auto again = state::write_snapshot("XCME", "r2", holdings(), &current);
    CHECK(again.relative == first.relative);
    CHECK(again.sha256 == first.sha256);
    CHECK(again.rows == first.rows);
    CHECK(fixture::files_under(state::state_dir() / "XCME") == std::vector<std::string>{"r1.alloc.parquet"});
}

TEST_CASE("snapshots: never overwritten, tampering and loss are refused") {
    fixture::Tree tree;
    auto snap = state::write_snapshot("XCME", "r1", holdings());
    auto changed = holdings();
    changed.assigned["C"] = 11;
    CHECK_THROWS_WITH_AS(state::write_snapshot("XCME", "r1", changed), doctest::Contains("never overwritten"),
                         state::StateCorrupt);
    auto target = state::snapshot_path(snap.relative);
    util::write_file(target, util::read_file(target) + "x");
    CHECK_THROWS_WITH_AS(state::load_snapshot("XCME", venue_head(10, snap)),
                         doctest::Contains("changed after it was written"), state::StateCorrupt);
    state::Snapshot gone{"XCME/gone.alloc.parquet", "x", 0};
    CHECK_THROWS_WITH_AS(state::load_snapshot("XCME", venue_head(10, gone)), doctest::Contains("does not exist"),
                         state::StateCorrupt);
}

TEST_CASE("counter floor: either the head or the scan alone is enough") {
    fixture::Tree tree;
    CHECK(state::global_counter(nullptr) == std::pair<std::int64_t, std::string>{0, ""});
    state::Head head;
    head.counter = 5000;
    CHECK(state::global_counter(&head) == std::pair<std::int64_t, std::string>{5000, state::HEAD_NAME});
    counter_token::write_sequence("20260925", counter_token::Sequence(138744));
    CHECK(state::global_counter(nullptr) == std::pair<std::int64_t, std::string>{138744, "20260925"});
    head.counter = 100;
    CHECK(state::global_counter(&head).first == 138744);
    head.counter = 200000;
    CHECK(state::global_counter(&head).first == 200000);
    CHECK(jsonio::read(counter_token::sequence_path("20260925"))["version"] == 8);
}

TEST_CASE("venue days and a day's allocation") {
    fixture::Tree tree;
    counter_token::write_venue_manifest("20260924", "XCME", {12, {{"A", 1}}, {}, {}});
    counter_token::write_venue_manifest("20260925", "XNSE", {16, {{"A", 1}}, {}, {}});
    counter_token::write_venue_manifest("20260926", "XCME", {12, {{"A", 1}}, {}, {}});
    CHECK(state::venue_days("xcme") == std::vector<std::string>{"20260924", "20260926"});

    fixture::Tree other;
    counter_token::write_venue_manifest("20260924", "XCME", {12, {{"A", 1}, {"B", 2}}, {5}, {}});
    auto alloc = state::day_alloc("20260924", "XCME");
    REQUIRE(alloc);
    CHECK(alloc->assigned == counter_token::TokenMap{{"A", 1}, {"B", 2}});
    CHECK(alloc->free == std::vector<std::int64_t>{5});
    CHECK_FALSE(state::day_alloc("20260925", "XCME"));
}

TEST_CASE("a 7.x manifest is refused by venue_entry until migrated") {
    fixture::Tree tree;
    counter_token::write_venue_manifest("20260924", "XCME", {12, {{"A", 1}}, {}, {}});
    auto path = counter_token::venue_manifest_path("20260924", "XCME");
    auto doc = jsonio::read(path);
    CHECK(doc["version"] == 8);
    doc["version"] = 5;
    util::write_file(path, jsonio::pretty(doc));
    CHECK_THROWS_WITH(counter_token::venue_entry("20260924", "XCME"), doctest::Contains("is a 7.x manifest (version 5)"));
    auto any = counter_token::venue_entry("20260924", "XCME", true);
    REQUIRE(any);
    CHECK(any->assigned == counter_token::TokenMap{{"A", 1}});
}
