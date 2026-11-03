#include "test_qa.hpp"

#include <doctest/doctest.h>

#include "premarket/qa.hpp"
#include "session_fixture.hpp"

using namespace premarket;
using namespace session_fixture;

namespace {

std::vector<std::string> hard_failures(const std::vector<qa::Check>& checks) {
    std::vector<std::string> out;
    for (const auto& c : checks)
        if (!c.ok && c.hard) out.push_back(c.day + " " + c.venue + " " + c.name + ": " + c.detail);
    return out;
}

bool any_contains(const std::vector<std::string>& lines, const std::string& needle) {
    return std::any_of(lines.begin(), lines.end(), [&](const std::string& l) { return l.find(needle) != std::string::npos; });
}

bool has_check(const std::vector<qa::Check>& checks, const std::string& name) {
    return std::any_of(checks.begin(), checks.end(), [&](const qa::Check& c) { return c.name == name; });
}

}  // namespace

TEST_CASE("check-tokens: a live chain passes") {
    Box box;
    init();
    numbered(numbering::LIVE, "20260926", "XCME", 12, {"A", "B", "C"});
    numbered(numbering::LIVE, "20260927", "XCME", 12, {"A", "C", "D"});
    auto checks = qa::token_checks({"20260926", "20260927"}, {"XCME"});
    CHECK(hard_failures(checks).empty());
    CHECK(has_check(checks, "pool drained first"));
}

TEST_CASE("check-tokens: a fill with a recorded clash passes") {
    Box box;
    init();
    numbered(numbering::LIVE, "20260926", "XCME", 12, {"K", "M"});
    numbered(numbering::LIVE, "20260928", "XCME", 12, {"K", "X"});
    numbered(numbering::FILL, "20260927", "XCME", 12, {"K", "M", "X"}, "missed day");
    auto checks = qa::token_checks({"20260926", "20260927", "20260928"}, {"XCME"});
    CHECK(hard_failures(checks).empty());
    CHECK(std::any_of(checks.begin(), checks.end(), [](const qa::Check& c) {
        return c.name == "stable" && c.detail.find("recorded as exceptions") != std::string::npos;
    }));
    for (const char* name : {"never releases", "fill provenance", "filled from"}) {
        CAPTURE(name);
        CHECK(has_check(checks, name));
    }
}

TEST_CASE("check-tokens: an unrecorded move fails stable") {
    Box box;
    init();
    numbered(numbering::LIVE, "20260926", "XCME", 12, {"A", "B"});
    auto plan = numbered(numbering::LIVE, "20260927", "XCME", 12, {"A", "B"});
    auto moved = plan.day.assigned;
    std::swap(moved.at("A"), moved.at("B"));
    write_normalized("20260927", "XCME", moved);
    auto failures = hard_failures(qa::token_checks({"20260926", "20260927"}, {"XCME"}));
    CHECK(std::any_of(failures.begin(), failures.end(), [](const std::string& f) {
        return f.find("stable") != std::string::npos && f.find("no recorded exception") != std::string::npos;
    }));
}

TEST_CASE("check-state: passes after normal runs, fails with no state") {
    {
        Box box;
        CHECK(any_contains(hard_failures(qa::state_checks()), "init-state"));
    }
    Box box;
    init();
    number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N9"});
    CHECK(hard_failures(qa::state_checks()).empty());
}

TEST_CASE("check-state: a day numbered outside a session is caught") {
    Box box;
    init();
    counter_token::write_venue_manifest("20260930", "XNSE", {16, {{"Q", 99}}, {}, {}});
    auto failures = hard_failures(qa::state_checks());
    CHECK(any_contains(failures, "newest on disk"));
    CHECK(any_contains(failures, "counter covers"));
}

TEST_CASE("check-state: a tampered snapshot is caught") {
    Box box;
    auto head = init();
    auto path = state::snapshot_path(head.venues.at("XNSE").snapshot);
    util::write_file(path, util::read_file(path) + "x");
    CHECK(any_contains(hard_failures(qa::state_checks()), "snapshot"));
}

TEST_CASE("check-state: an unclosed run is a warning, not a failure") {
    Box box;
    init();
    state::append_event(state::event("START", "r-open", "normalize", jsonio::Json::object()));
    auto checks = qa::state_checks();
    auto runs = std::find_if(checks.begin(), checks.end(), [](const qa::Check& c) { return c.name == "runs closed"; });
    REQUIRE(runs != checks.end());
    CHECK_FALSE(runs->ok);
    CHECK_FALSE(runs->hard);
    CHECK(hard_failures(checks).empty());
}
