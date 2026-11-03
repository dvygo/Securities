#include "test_numbering_session.hpp"

#include <doctest/doctest.h>

#include <cstdlib>

#include "session_fixture.hpp"

using namespace premarket;
using numbering_session::NumberingRefused;
using numbering_session::Session;

using namespace session_fixture;

TEST_CASE("init-state builds the head from the newest manifests, once") {
    Box box;
    auto head = init();
    CHECK(head.counter == 3);
    CHECK(head.venues.at("XNSE").newest == "20260925");
    CHECK(head.venues.at("XNSE").live_date == "20260925");
    CHECK(state::load_snapshot("XNSE", head.venues.at("XNSE")).assigned ==
          counter_token::TokenMap{{"N1", 1}, {"N2", 2}, {"N3", 3}});
    CHECK_FALSE(head.venues.count("XCME"));
    CHECK_THROWS_WITH_AS(init(), doctest::Contains("already exists"), state::StateCorrupt);
}

TEST_CASE("init-state: a dry run writes nothing") {
    Box box;
    CHECK_FALSE(state::init_state("test", true, fixture::quiet));
    CHECK_FALSE(fs::exists(state::state_dir()));
}

TEST_CASE("init-state refuses venues that share a token") {
    Box box;
    counter_token::write_venue_manifest("20260924", "XCME", {12, {{"C1", 2}}, {}, {}});
    CHECK_THROWS_WITH_AS(init(), doctest::Contains("share 1 token"), state::StateCorrupt);
}

TEST_CASE("init-state takes a backup and logs the run") {
    Box box;
    init("adopt the state store");
    CHECK(backups() == 1);
    CHECK(event_types() == std::vector<std::string>{"START", "COMPLETE"});
    CHECK(state::read_events().back()["run"]["facets"]["premarket"]["reason"] == "adopt the state store");
}

TEST_CASE("guards: no head, a second session, the wrong date for the mode, a changed venue_id") {
    Box box;
    CHECK_THROWS_WITH_AS(in_session(numbering::LIVE, [](Session&) {}), doctest::Contains("init-state"),
                         state::StateMissing);
    init();
    in_session(numbering::LIVE, [](Session&) {
        CHECK_THROWS_WITH_AS(in_session(numbering::LIVE, [](Session&) {}), doctest::Contains("another numbering run"),
                             state::LockHeld);
    });
    in_session(numbering::LIVE, [](Session& s) {
        CHECK_THROWS_WITH_AS(s.venue("20260924", "XNSE", 16), doctest::Contains("--dates"), NumberingRefused);
        CHECK_THROWS_WITH_AS(s.venue("20260926", "XNSE", 99), doctest::Contains("venue_id"), NumberingRefused);
    });
    in_session(numbering::FILL, [](Session& s) {
        CHECK_THROWS_WITH_AS(s.venue("20260925", "XNSE", 16), doctest::Contains("--date-dir"), NumberingRefused);
    }, "t");
}

TEST_CASE("guards: a live run refuses a filled newest day") {
    Box box;
    init();
    number(numbering::FILL, "20260924", "XCME", 12, {"C1", "C2"});
    in_session(numbering::LIVE, [](Session& s) {
        CHECK_THROWS_WITH_AS(s.venue("20260924", "XCME", 12), doctest::Contains("filled"), NumberingRefused);
        CHECK_THROWS_WITH_AS(s.venue("20260923", "XCME", 12), doctest::Contains("older"), NumberingRefused);
    });
}

// The collision the session exists to prevent: 20260925 numbered first, 20260924 filled
// after, then XCME live on the 25th. No token may be shared by two venues on a date.
TEST_CASE("a backfilled XCME never takes a number XNSE holds") {
    Box box;
    init();
    auto fill = number(numbering::FILL, "20260924", "XCME", 12, {"C1", "C2", "C3"});
    std::int64_t lowest = INT64_MAX;
    for (const auto& [s, t] : fill.plan.day.assigned) lowest = std::min(lowest, t);
    CHECK(lowest > 3);
    auto live = number(numbering::LIVE, "20260925", "XCME", 12, {"C1", "C2", "C4"});
    std::set<std::int64_t> xnse;
    for (const auto& [s, t] : assigned("20260925", "XNSE")) xnse.insert(t);
    std::set<std::int64_t> xcme;
    for (const auto& [s, t] : live.plan.day.assigned) {
        CHECK_FALSE(xnse.count(t));
        xcme.insert(t);
    }
    CHECK(live.plan.day.assigned.at("C1") == fill.plan.day.assigned.at("C1"));
    CHECK(xcme.size() == 3);
}

TEST_CASE("commit: a live run writes every artifact and logs it") {
    Box box;
    init();
    auto run = number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N4"});
    CHECK(run.plan.day.assigned.at("N4") == 3);
    CHECK(assigned("20260926", "XNSE") == run.plan.day.assigned);
    auto header = jsonio::read(counter_token::venue_manifest_path("20260926", "XNSE"));
    CHECK(header["version"] == 8);
    const auto& block = header["numbering"];
    CHECK(block["mode"] == "live");
    CHECK(block["reason"] == "test");
    auto head = state::read_head();
    CHECK(head->venues.at("XNSE").snapshot == block["state_after"]["snapshot"].get<std::string>());
    CHECK(head->venues.at("XNSE").live_date == "20260926");
    CHECK(head->counter == 3);
    CHECK(counter_token::load_sequence("20260926") == 3);
    CHECK(fixture::files_ending(box.data(), state::STAGED).empty());
    auto events = state::read_events();
    REQUIRE(events.size() >= 3);
    std::vector<std::pair<std::string, std::string>> last;
    for (auto i = events.size() - 3; i < events.size(); ++i)
        last.emplace_back(events[i]["eventType"].get<std::string>(), events[i]["job"]["name"].get<std::string>());
    CHECK(last == std::vector<std::pair<std::string, std::string>>{
                      {"START", "normalize"}, {"COMPLETE", "normalize"}, {"COMPLETE", "normalize"}});
    CHECK(run.outcomes.at(0).status == "done");
}

// The M/N clash through a whole session: M leaves on the 28th and X takes its number, then
// the 27th is filled holding K, M and X. X keeps the number it holds on the 28th, M cannot,
// and the exception file beside the allocation says so.
TEST_CASE("commit: a fill records its exceptions beside the allocation") {
    Box box;
    init();
    number(numbering::LIVE, "20260926", "XCME", 12, {"K", "M"});
    number(numbering::LIVE, "20260928", "XCME", 12, {"K", "X"});
    auto m = assigned("20260926", "XCME").at("M");
    auto x = assigned("20260928", "XCME").at("X");
    CHECK(m == x);
    auto fill = number(numbering::FILL, "20260927", "XCME", 12, {"K", "M", "X"}, "missed day");
    auto rows = counter_token::read_exceptions("20260927", "XCME");
    REQUIRE(rows.size() == 1);
    CHECK(rows[0].script == "M");
    CHECK(rows[0].wanted_from == "20260926");
    CHECK(rows[0].lost_to == "X");
    CHECK(fill.plan.day.assigned.at("X") == x);
    CHECK(fill.plan.day.assigned.at("M") != m);
}

TEST_CASE("commit: a fill takes a backup first, and backups are pruned to ten") {
    Box box;
    init();
    auto before = backups();
    number(numbering::FILL, "20260924", "XCME", 12, {"C1"});
    CHECK(backups() == before + 1);
    for (int day = 10; day < 22; ++day) number(numbering::FILL, "202609" + std::to_string(day), "XCME", 12, {"C1"});
    CHECK(backups() == static_cast<std::size_t>(state::BACKUPS_KEEP));
}

TEST_CASE("commit: a backup restores the manifests it took") {
    Box box;
    init();
    auto header = counter_token::venue_manifest_path("20260925", "XNSE");
    auto original = util::read_file(header);
    auto archive = state::backup("manual").first;
    util::write_file(header, "{}");
    std::string cmd = "tar -xzf '" + archive.string() + "' -C '" + box.data().string() + "'";
    REQUIRE(std::system(cmd.c_str()) == 0);
    CHECK(util::read_file(header) == original);
}

TEST_CASE("commit: a rerun that changes nothing reuses the snapshot") {
    Box box;
    init();
    number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2"});
    auto first = state::read_head()->venues.at("XNSE").snapshot;
    number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2"});
    CHECK(state::read_head()->venues.at("XNSE").snapshot == first);
}

TEST_CASE("crash before the commit point: numbers leak, nothing else survives") {
    Box box;
    init();
    std::vector<std::string> everyone = {"N1", "N2", "N3", "N4", "N5"};
    {
        fixture::Failpoint fp(fixture::crash_at("write_head"));
        CHECK_THROWS_WITH(number(numbering::LIVE, "20260926", "XNSE", 16, everyone), doctest::Contains("simulated"));
    }
    CHECK_FALSE(fixture::files_ending(box.data(), state::STAGED).empty());
    CHECK(counter_token::load_sequence("20260926") == 5);
    auto again = number(numbering::LIVE, "20260926", "XNSE", 16, everyone);
    CHECK(fixture::files_ending(box.data(), state::STAGED).empty());
    CHECK(again.plan.day.assigned.at("N4") == 6);
    CHECK(again.plan.day.assigned.at("N5") == 7);
    auto types = event_types();
    CHECK(std::count(types.begin(), types.end(), "FAIL") == 1);
}

TEST_CASE("crash after the commit point: recovery finishes it") {
    Box box;
    init();
    {
        fixture::Failpoint fp(fixture::crash_at("write_header"));
        CHECK_THROWS_WITH(number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N3", "N4"}),
                          doctest::Contains("simulated"));
    }
    auto head = state::read_head();
    CHECK(head->last_commit["date"] == "20260926");
    CHECK_FALSE(fs::exists(counter_token::venue_manifest_path("20260926", "XNSE")));
    auto actions = state::recover(*head, fixture::quiet);
    CHECK(std::any_of(actions.begin(), actions.end(),
                      [](const std::string& a) { return a.find("wrote the XNSE 20260926 header") != std::string::npos; }));
    CHECK(assigned("20260926", "XNSE") == counter_token::TokenMap{{"N1", 1}, {"N2", 2}, {"N3", 3}, {"N4", 4}});
}

TEST_CASE("crash after the commit point: the next session recovers on its own") {
    Box box;
    init();
    {
        fixture::Failpoint fp(fixture::crash_at("write_header"));
        CHECK_THROWS(number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N3", "N4"}));
    }
    in_session(numbering::LIVE, [](Session&) {});
    CHECK(assigned("20260926", "XNSE").at("N4") == 4);
}

TEST_CASE("crash between the commit point and the promote: recovery promotes") {
    Box box;
    init();
    {
        bool fired = false;
        fixture::Failpoint fp([&](const std::string& name) {
            if (name == "promote" && !fired) {
                fired = true;
                throw RuntimeError("simulated crash in promote");
            }
        });
        CHECK_THROWS(number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N3", "N4"}));
    }
    auto actions = state::recover(*state::read_head(), fixture::quiet);
    CHECK(std::any_of(actions.begin(), actions.end(), [](const std::string& a) { return util::starts_with(a, "promoted"); }));
    CHECK(fixture::files_ending(box.data(), state::STAGED).empty());
    CHECK(assigned("20260926", "XNSE").at("N4") == 4);
}

TEST_CASE("a tampered staged file is never promoted") {
    Box box;
    init();
    {
        fixture::Failpoint fp(fixture::crash_at("promote"));
        CHECK_THROWS(number(numbering::LIVE, "20260926", "XNSE", 16, {"N1", "N2", "N3", "N4"}));
    }
    auto head = state::read_head();
    auto staged = box.data() / head->last_commit["staged"][0]["staged"].get<std::string>();
    REQUIRE(fs::exists(staged));
    util::write_file(staged, "tampered");
    CHECK_THROWS_WITH_AS(state::recover(*head, fixture::quiet), doctest::Contains("digest"), state::StateCorrupt);
}

TEST_CASE("a preview writes nothing and chains its own plans") {
    Box box;
    init();
    auto before = fixture::files_under(box.data());
    std::vector<numbering_session::Outcome> outcomes;
    in_session(numbering::FILL, [&](Session& s) {
        for (const auto& [date, scripts] : std::vector<std::pair<std::string, std::vector<std::string>>>{
                 {"20260924", {"C1", "C2"}}, {"20260923", {"C1", "C9"}}}) {
            auto venue = s.venue(date, "XCME", 12);
            auto plan = venue.plan(scripts);
            venue.reserve(plan);
            venue.commit(plan);
        }
        outcomes = s.outcomes;
    }, "t", true);
    CHECK(fixture::files_under(box.data()) == before);
    REQUIRE(outcomes.size() == 2);
    CHECK(outcomes[0].status == "previewed");
    CHECK(outcomes[1].status == "previewed");
    CHECK(util::starts_with(outcomes[1].detail, "2 scripts"));
}

TEST_CASE("a preview draws what the real run would") {
    Box box;
    init();
    counter_token::TokenMap planned;
    in_session(numbering::FILL, [&](Session& s) {
        auto venue = s.venue("20260924", "XCME", 12);
        planned = venue.plan({"C1", "C2"}).day.assigned;
    }, "t", true);
    auto real = number(numbering::FILL, "20260924", "XCME", 12, {"C1", "C2"});
    CHECK(planned == real.plan.day.assigned);
}
