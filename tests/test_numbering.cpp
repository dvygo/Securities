#include "test_numbering.hpp"

#include <doctest/doctest.h>
#include <zlib.h>

#include <chrono>
#include <functional>
#include <random>
#include <set>

#include "premarket/normalize/numbering.hpp"
#include "premarket/jsonio.hpp"
#include "premarket/util.hpp"

using namespace premarket;
using numbering::DayAlloc;
using numbering::Holdings;
using numbering::Plan;
using counter_token::Sequence;
using counter_token::TokenMap;

namespace {

Holdings holdings(const TokenMap& assigned, const std::string& date, std::vector<std::int64_t> free = {}) {
    Holdings h;
    h.venue_id = 1;
    h.assigned = assigned;
    std::sort(free.begin(), free.end());
    h.free = free;
    for (const auto& [s, t] : assigned) h.last_date[s] = date;
    return h;
}

DayAlloc day(const std::string& date, const TokenMap& assigned, const TokenMap& retained = {},
             std::vector<std::int64_t> free = {}) {
    std::sort(free.begin(), free.end());
    return DayAlloc{date, assigned, retained, free};
}

}  // namespace

// Rule tests use the worked examples from the 7.x design discussion, so a failure reads as a
// story. They are the Python suite's, assertion for assertion.

TEST_CASE("fill: a backward fill keeps the later day's tokens") {
    // History before the first day. C keeps its token; A and F, gone by the 25th, take new
    // numbers because nothing is free.
    auto state = holdings({{"B", 5}, {"C", 6}, {"E", 8}}, "20260925");
    auto later = day("20260925", {{"B", 5}, {"C", 6}, {"E", 8}});
    Sequence seq(8);
    auto plan = numbering::allocate_fill("20260924", {"A", "C", "F"}, nullptr, nullptr, &later, &state, seq, 1);
    CHECK(plan.day.assigned == TokenMap{{"A", 9}, {"C", 6}, {"F", 10}});
    CHECK(plan.exceptions.empty());
    CHECK(plan.neighbours == std::map<std::string, std::string>{{"earlier", ""}, {"later", "20260925"}});
}

TEST_CASE("fill: adds holdings and changes none") {
    // Rule 5: the state after a fill is the state before, plus holdings for scripts that held
    // nothing. B and E keep their tokens for the next live day.
    auto state = holdings({{"B", 5}, {"C", 6}, {"E", 8}}, "20260925");
    auto later = day("20260925", {{"B", 5}, {"C", 6}, {"E", 8}});
    Sequence seq(8);
    auto plan = numbering::allocate_fill("20260924", {"A", "C", "F"}, nullptr, nullptr, &later, &state, seq, 1);
    for (const auto& [s, t] : state.assigned) CHECK(plan.state_after.assigned.at(s) == t);
    CHECK(plan.state_after.assigned.at("A") == 9);
    CHECK(plan.state_after.assigned.at("F") == 10);
    CHECK(plan.state_after.last_date.at("A") == "20260924");
    CHECK(plan.state_after.last_date.at("B") == "20260925");
}

TEST_CASE("fill: a clash goes to the later day and is recorded") {
    // Tuesday M=7; Thursday, M gone, X listed on Wednesday took 7. Filling Wednesday, both are
    // present: X keeps 7, M gets a new number, on record.
    auto tue = day("20260922", {{"K", 3}, {"M", 7}});
    auto thu = day("20260924", {{"K", 3}, {"X", 7}});
    auto state = holdings({{"K", 3}, {"X", 7}}, "20260924");
    Sequence seq(7);
    auto plan = numbering::allocate_fill("20260923", {"K", "M", "X"}, nullptr, &tue, &thu, &state, seq, 1);
    CHECK(plan.day.assigned == TokenMap{{"K", 3}, {"M", 8}, {"X", 7}});
    REQUIRE(plan.exceptions.size() == 1);
    const auto& e = plan.exceptions[0];
    CHECK(e.script == "M");
    CHECK(e.token == 8);
    CHECK(e.wanted == 7);
    CHECK(e.wanted_from == "20260922");
    CHECK(e.lost_to == "X");
}

TEST_CASE("fill: neighbours that disagree resolve to the later one, on record") {
    auto tue = day("20260922", {{"S", 4}});
    auto thu = day("20260924", {{"S", 9}});
    auto state = holdings({{"S", 9}}, "20260924");
    Sequence seq(9);
    auto plan = numbering::allocate_fill("20260923", {"S"}, nullptr, &tue, &thu, &state, seq, 1);
    CHECK(plan.day.assigned == TokenMap{{"S", 9}});
    REQUIRE(plan.exceptions.size() == 1);
    CHECK(plan.exceptions[0].wanted == 4);
    CHECK(plan.exceptions[0].wanted_from == "20260922");
    CHECK(plan.exceptions[0].lost_to == "chain:20260924");
}

TEST_CASE("fill: with only an earlier neighbour it carries forward") {
    auto earlier = day("20260922", {{"A", 1}});
    auto state = holdings({{"A", 1}}, "20260922");
    Sequence seq(1);
    auto plan = numbering::allocate_fill("20260923", {"A", "B"}, nullptr, &earlier, nullptr, &state, seq, 1);
    CHECK(plan.day.assigned == TokenMap{{"A", 1}, {"B", 2}});
}

TEST_CASE("fill: with no neighbour and no state everything is new") {
    Sequence seq(138744);
    auto plan = numbering::allocate_fill("20260920", {"A", "B"}, nullptr, nullptr, nullptr, nullptr, seq, 12);
    CHECK(plan.day.assigned == TokenMap{{"A", 138745}, {"B", 138746}});
    CHECK(plan.state_after.assigned == plan.day.assigned);
}

TEST_CASE("fill: a re-run of a filled day changes nothing and draws nothing") {
    auto state = holdings({{"B", 5}, {"C", 6}, {"E", 8}}, "20260925");
    auto later = day("20260925", {{"B", 5}, {"C", 6}, {"E", 8}});
    Sequence seq(8);
    auto first = numbering::allocate_fill("20260924", {"A", "C", "F"}, nullptr, nullptr, &later, &state, seq, 1);
    auto issued = seq.issued();
    auto again =
        numbering::allocate_fill("20260924", {"A", "C", "F"}, &first.day, nullptr, &later, &first.state_after, seq, 1);
    CHECK(again.day.assigned == first.day.assigned);
    CHECK(seq.issued() == issued);
    CHECK(again.state_after.assigned == first.state_after.assigned);
}

TEST_CASE("fill: a script added on re-run still takes its neighbour's token") {
    // Review finding R4: the day's own manifest is the source only for its own scripts. B,
    // missing from the first fill, is on the later day -- it must take the later day's token.
    auto state = holdings({{"B", 5}, {"C", 6}}, "20260925");
    auto later = day("20260925", {{"B", 5}, {"C", 6}});
    Sequence seq(6);
    auto first = numbering::allocate_fill("20260924", {"C"}, nullptr, nullptr, &later, &state, seq, 1);
    auto again = numbering::allocate_fill("20260924", {"B", "C"}, &first.day, nullptr, &later, &first.state_after, seq, 1);
    CHECK(again.day.assigned == TokenMap{{"B", 5}, {"C", 6}});
    CHECK(again.exceptions.empty());
}

TEST_CASE("fill: a script dropped on re-run is retained for the date") {
    auto state = holdings({{"C", 6}}, "20260925");
    auto later = day("20260925", {{"C", 6}});
    Sequence seq(6);
    auto first = numbering::allocate_fill("20260924", {"A", "C"}, nullptr, nullptr, &later, &state, seq, 1);
    auto again = numbering::allocate_fill("20260924", {"C"}, &first.day, nullptr, &later, &first.state_after, seq, 1);
    CHECK(again.day.retained == TokenMap{{"A", first.day.assigned.at("A")}});
    auto a = first.day.assigned.at("A");
    CHECK(std::find(again.day.free.begin(), again.day.free.end(), a) == again.day.free.end());
}

TEST_CASE("fill: a relisted script uses its state token") {
    // R on the 23rd and in today's state, but not on the 24th: it takes the token it holds.
    auto state = holdings({{"C", 6}, {"R", 11}}, "20260925");
    auto later = day("20260924", {{"C", 6}});
    Sequence seq(11);
    auto plan = numbering::allocate_fill("20260923", {"C", "R"}, nullptr, nullptr, &later, &state, seq, 1);
    CHECK(plan.day.assigned == TokenMap{{"C", 6}, {"R", 11}});
}

TEST_CASE("fill: a relisted script's taken token is never overwritten in the state") {
    // The bug the 7.x simulation found. R holds 7 in the state, but on this day 7 belongs to X
    // (kept from the later day). R takes a day-local number; the state must keep R=7, or R's
    // live token changes at the next live run.
    auto state = holdings({{"R", 7}, {"X", 3}}, "20260925");
    auto later = day("20260924", {{"X", 7}});
    Sequence seq(11);
    auto plan = numbering::allocate_fill("20260923", {"R", "X"}, nullptr, nullptr, &later, &state, seq, 1);
    CHECK(plan.day.assigned.at("X") == 7);
    CHECK(plan.day.assigned.at("R") == 12);
    CHECK(plan.state_after.assigned.at("R") == 7);
    CHECK(plan.retired == std::vector<std::int64_t>{12});
}

TEST_CASE("fill: a free token a neighbour hands to someone else is skipped") {
    // Review finding R6: 4 is free in the state but the earlier day gave it to Q. Handing it
    // to N here would set up a clash the day a fill lands between.
    auto state = holdings({{"K", 3}}, "20260925", {4, 5});
    auto earlier = day("20260922", {{"K", 3}, {"Q", 4}});
    Sequence seq(9);
    auto plan = numbering::allocate_fill("20260923", {"K", "N"}, nullptr, &earlier, nullptr, &state, seq, 1);
    CHECK(plan.day.assigned.at("N") == 5);
    CHECK(plan.state_after.free == std::vector<std::int64_t>{4});
}

TEST_CASE("fill: a kept token sitting in free is not handed to anyone else") {
    // K's token 3 was released since the later day, so it is free in the state. K keeps it
    // here; no other script on this day may take it.
    auto state = holdings({}, "20260925", {3, 4});
    auto later = day("20260924", {{"K", 3}});
    Sequence seq(9);
    auto plan = numbering::allocate_fill("20260923", {"K", "Z"}, nullptr, nullptr, &later, &state, seq, 1);
    CHECK(plan.day.assigned.at("K") == 3);
    CHECK(plan.day.assigned.at("Z") == 4);
}

TEST_CASE("live: the first day numbers from the counter") {
    Sequence seq(138744);
    auto plan = numbering::allocate_live("20260925", {"B", "A"}, nullptr, nullptr, seq, 12);
    CHECK(plan.day.assigned == TokenMap{{"A", 138745}, {"B", 138746}});
}

TEST_CASE("live: the live day after a fill keeps every live token") {
    // The E example end to end: the state after the 24th's fill holds A and F too; the 26th
    // keeps B, C, E and releases A and F.
    auto state = holdings({{"B", 5}, {"C", 6}, {"E", 8}}, "20260925");
    auto live25 = day("20260925", {{"B", 5}, {"C", 6}, {"E", 8}});
    Sequence seq(8);
    auto fill = numbering::allocate_fill("20260924", {"A", "C", "F"}, nullptr, nullptr, &live25, &state, seq, 1);
    auto plan = numbering::allocate_live("20260926", {"B", "C", "D", "E"}, &fill.state_after, &live25, seq, 1);
    CHECK(plan.day.assigned.at("B") == 5);
    CHECK(plan.day.assigned.at("C") == 6);
    CHECK(plan.day.assigned.at("E") == 8);
    CHECK(plan.day.assigned.at("D") == 9);
    CHECK(plan.exceptions.empty());
}

TEST_CASE("live: a same-day re-run with fewer scripts retains rather than frees") {
    // Decision 20 of the 7.x design: numbering is append-only within a date.
    Sequence seq;
    auto first = numbering::allocate_live("20260925", {"A", "B", "C"}, nullptr, nullptr, seq, 1);
    auto again = numbering::allocate_live("20260925", {"A", "B"}, &first.state_after, nullptr, seq, 1);
    CHECK(again.day.retained == TokenMap{{"C", 3}});
    CHECK(std::find(again.day.free.begin(), again.day.free.end(), 3) == again.day.free.end());
    CHECK(again.state_after.assigned.at("C") == 3);
}

TEST_CASE("live: a mid-day vendor switch never reuses a token on that date") {
    // NSE numbers the market at 09:00; Fyers, with different coverage, at 14:00. Nothing Fyers
    // lacks may lose its number to something only Fyers has.
    Sequence seq;
    auto nse = numbering::allocate_live("20260925", {"A", "B", "N1"}, nullptr, nullptr, seq, 1);
    auto fyers = numbering::allocate_live("20260925", {"A", "B", "F1"}, &nse.state_after, nullptr, seq, 1);
    std::map<std::int64_t, std::string> seen;
    for (const Plan* p : {&nse, &fyers})
        for (const auto& [s, t] : p->day.assigned) CHECK(seen.emplace(t, s).first->second == s);
    CHECK(fyers.day.assigned.at("F1") != nse.day.assigned.at("N1"));
}

TEST_CASE("live: a retained token is freed on the next date") {
    Sequence seq;
    auto first = numbering::allocate_live("20260925", {"A", "B", "C"}, nullptr, nullptr, seq, 1);
    auto again = numbering::allocate_live("20260925", {"A", "B"}, &first.state_after, nullptr, seq, 1);
    auto next = numbering::allocate_live("20260926", {"A", "B", "D"}, &again.state_after, &again.day, seq, 1);
    CHECK(next.day.assigned.at("D") == 3);
}

TEST_CASE("live: a returning script reclaims and is not an exception") {
    Sequence seq;
    auto day1 = numbering::allocate_live("20260924", {"A", "C", "E"}, nullptr, nullptr, seq, 1);
    auto truncated = numbering::allocate_live("20260925", {"A", "E"}, &day1.state_after, &day1.day, seq, 1);
    auto rerun =
        numbering::allocate_live("20260925", {"A", "B2", "C", "E"}, &truncated.state_after, &day1.day, seq, 1);
    CHECK(rerun.day.assigned.at("C") == day1.day.assigned.at("C"));
    CHECK(rerun.exceptions.empty());
}

TEST_CASE("live: a same-day re-run is idempotent") {
    Sequence seq;
    auto first = numbering::allocate_live("20260925", {"A", "B"}, nullptr, nullptr, seq, 1);
    auto issued = seq.issued();
    auto again = numbering::allocate_live("20260925", {"A", "B"}, &first.state_after, nullptr, seq, 1);
    CHECK(again.day.assigned == first.day.assigned);
    CHECK(seq.issued() == issued);
}

// A real XCME day is 1.09M scripts and OPRA about 2M. Anything quadratic in the allocators
// takes hours there -- a list membership test did, on 7.x's first rehearsal against real
// data. 300k scripts must stay well inside the Python suite's 10 s budget.
TEST_CASE("scale: a live re-run at 300k scripts is linear" * doctest::skip(PREMARKET_SANITIZED)) {
    std::vector<std::string> scripts;
    for (int i = 0; i < 300000; ++i) scripts.push_back("S" + util::zero_pad(i, 7));
    Sequence seq;
    auto first = numbering::allocate_live("20260925", scripts, nullptr, nullptr, seq, 12);
    auto started = std::chrono::steady_clock::now();
    std::vector<std::string> fewer(scripts.begin(), scripts.end() - 1000);
    auto again = numbering::allocate_live("20260925", fewer, &first.state_after, nullptr, seq, 12);
    CHECK(std::chrono::steady_clock::now() - started < std::chrono::seconds(10));
    CHECK(again.day.retained.size() == 1000);
}

TEST_CASE("scale: a fill at 300k scripts is linear" * doctest::skip(PREMARKET_SANITIZED)) {
    std::vector<std::string> scripts;
    for (int i = 0; i < 300000; ++i) scripts.push_back("S" + util::zero_pad(i, 7));
    Sequence seq;
    auto live = numbering::allocate_live("20260925", scripts, nullptr, nullptr, seq, 12);
    std::vector<std::string> older(scripts.begin() + 5000, scripts.end());
    for (int i = 0; i < 5000; ++i) older.push_back("X" + util::zero_pad(i, 7));
    auto started = std::chrono::steady_clock::now();
    auto plan = numbering::allocate_fill("20260924", older, nullptr, nullptr, &live.day, &live.state_after, seq, 12);
    CHECK(std::chrono::steady_clock::now() - started < std::chrono::seconds(10));
    CHECK(plan.counts.at("from_later") == 300000 - 5000);
}

// -- the property test: the simulation that found 7.x's design bugs ---------------------
//
// Random instrument universes (listings and expiries every day, and about one instrument in
// ten missing for a single day mid-life -- a vendor hiccup, a suspension -- then back), numbered
// through schedules of live runs, fills, re-runs and same-date vendor switches, with the whole
// contract checked after each history. The generator is std::mt19937 rather than Python's
// random, so the histories differ from 7.x's; the rules they are held to do not. Exact
// agreement with 7.x is the golden replay's job, further down.

namespace {

using LiveFn = std::function<Plan(const std::string&, const std::vector<std::string>&, const Holdings*,
                                  const DayAlloc*, Sequence&, std::int64_t)>;
using FillFn = std::function<Plan(const std::string&, const std::vector<std::string>&, const DayAlloc*,
                                  const DayAlloc*, const DayAlloc*, const Holdings*, Sequence&, std::int64_t)>;

struct Universe {
    std::map<std::string, std::pair<int, int>> listings;
    std::map<std::string, int> gaps;
    std::vector<std::string> on(int d) const {
        std::vector<std::string> out;
        for (const auto& [s, span] : listings) {
            auto g = gaps.find(s);
            if (span.first <= d && d <= span.second && !(g != gaps.end() && g->second == d)) out.push_back(s);
        }
        return out;
    }
};

Universe universe(std::mt19937& rng, int days) {
    auto randint = [&](int a, int b) { return std::uniform_int_distribution<int>(a, b)(rng); };
    auto random01 = [&] { return std::uniform_real_distribution<double>(0.0, 1.0)(rng); };
    Universe u;
    int number = 0;
    for (int d = 0; d < days; ++d) {
        int count = d ? randint(5, 25) : 80;
        for (int i = 0; i < count; ++i) {
            ++number;
            std::string script = "S" + util::zero_pad(number, 4);
            int first = d ? d : -randint(0, 5);
            int last = d + randint(0, 6);
            u.listings[script] = {first, last};
            if (last - first >= 2 && random01() < 0.1) u.gaps[script] = randint(first + 1, last - 1);
        }
    }
    return u;
}

std::string date_of(int d) { return "202609" + util::zero_pad(d + 1, 2); }

using Schedule = std::vector<std::pair<std::string, int>>;

std::map<std::string, Schedule> schedules() {
    Schedule backward = {{"live", 7}};
    for (int d : {6, 5, 4, 3, 2, 1, 0}) backward.push_back({"fill", d});
    for (int d = 8; d < 14; ++d) backward.push_back({"live", d});
    Schedule gaps_newest, gaps_oldest;
    for (int d : {0, 1, 2, 4, 5, 8, 9, 10}) {
        gaps_newest.push_back({"live", d});
        gaps_oldest.push_back({"live", d});
    }
    for (int d : {7, 6, 3}) gaps_newest.push_back({"fill", d});
    for (int d : {3, 6, 7}) gaps_oldest.push_back({"fill", d});
    for (int d : {11, 12, 13}) {
        gaps_newest.push_back({"live", d});
        gaps_oldest.push_back({"live", d});
    }
    return {
        {"backward newest-first before live", backward},
        {"gaps filled newest-first", gaps_newest},
        {"gaps filled oldest-first", gaps_oldest},
        {"gap filled across separate runs",
         {{"live", 0}, {"live", 1}, {"live", 2}, {"live", 5}, {"live", 6}, {"fill", 4}, {"live", 7}, {"fill", 3}, {"live", 8}}},
        {"fills interleaved with live runs",
         {{"live", 5}, {"fill", 4}, {"live", 6}, {"fill", 3}, {"live", 8}, {"fill", 7}, {"fill", 2}, {"live", 9},
          {"fill", 1}, {"live", 10}}},
        {"re-runs of filled days",
         {{"live", 5}, {"fill", 4}, {"fill", 3}, {"live", 6}, {"refill", 4}, {"refill", 3}, {"live", 7}, {"refill", 4}}},
        {"mid-day vendor switches, then a backfill",
         {{"live", 1}, {"switch", 1}, {"live", 1}, {"live", 2}, {"switch", 2}, {"fill", 0}, {"live", 3}, {"switch", 3}}},
    };
}

bool switched_out(int seed, const std::string& script) {
    std::string key = std::to_string(seed) + ":" + script;
    return crc32(0L, reinterpret_cast<const Bytef*>(key.data()), static_cast<uInt>(key.size())) % 5 == 0;
}

std::vector<std::string> run_history(int seed, const Schedule& schedule, const LiveFn& live, const FillFn& fill,
                                     int days = 14) {
    std::mt19937 rng(static_cast<unsigned>(seed));
    auto u = universe(rng, days);
    Sequence sequence;
    std::optional<Holdings> state;
    std::map<std::string, DayAlloc> done;
    std::map<std::string, std::vector<numbering::TokenException>> exceptions;
    std::vector<std::int64_t> issued;
    std::map<std::string, std::map<std::int64_t, std::string>> handed;
    std::vector<std::string> problems;

    for (const auto& [op, d] : schedule) {
        std::string date = date_of(d);
        std::int64_t before = sequence.issued();
        auto scripts = u.on(d);
        if (op == "switch") {
            std::vector<std::string> kept;
            for (const auto& s : scripts)
                if (!switched_out(seed, s)) kept.push_back(s);
            scripts = kept;
        }
        Plan plan;
        std::optional<Holdings> prior = state;
        if (op == "live" || op == "switch") {
            std::optional<std::string> previous;
            for (const auto& [x, a] : done)
                if (x < date) previous = x;
            const DayAlloc* prev = previous ? &done.at(*previous) : nullptr;
            plan = live(date, scripts, state ? &*state : nullptr, prev, sequence, 1);
            if (prior && op == "live")
                for (const auto& s : scripts)
                    if (prior->assigned.count(s) && plan.day.assigned.at(s) != prior->assigned.at(s))
                        problems.push_back("rule 5: live " + date + " moved held " + s);
        } else {
            std::vector<std::string> numbered;
            for (const auto& [x, a] : done)
                if (x != date) numbered.push_back(x);
            auto [earlier, later] = numbering::neighbours(date, numbered);
            auto get = [&](const std::optional<std::string>& k) -> const DayAlloc* {
                return k && done.count(*k) ? &done.at(*k) : nullptr;
            };
            plan = fill(date, scripts, get(date), get(earlier), get(later), state ? &*state : nullptr, sequence, 1);
            if (prior)
                for (const auto& [s, t] : prior->assigned) {
                    auto it = plan.state_after.assigned.find(s);
                    if (it == plan.state_after.assigned.end() || it->second != t)
                        problems.push_back("rule 5: fill " + date + " altered held " + s);
                }
            if (op == "refill" && sequence.issued() != before)
                problems.push_back("rule 4: re-run of " + date + " drew numbers");
        }
        for (const auto& [s, t] : plan.day.assigned)
            if (handed[date].emplace(t, s).first->second != s)
                problems.push_back("rule 1: " + date + " token " + std::to_string(t) + " handed to two scripts");
        for (auto n = before + 1; n <= sequence.issued(); ++n) issued.push_back(n);
        state = plan.state_after;
        done[date] = plan.day;
        exceptions[date] = plan.exceptions;
        std::set<std::int64_t> held;
        for (const auto& [s, t] : state->assigned) held.insert(t);
        for (auto t : state->free)
            if (held.count(t)) {
                problems.push_back("state overlap after " + date);
                break;
            }
    }
    std::set<std::int64_t> unique(issued.begin(), issued.end());
    if (unique.size() != issued.size()) problems.push_back("rule 3: a number was issued twice");
    std::vector<std::string> ordered;
    for (const auto& [x, a] : done) ordered.push_back(x);
    for (std::size_t i = 0; i + 1 < ordered.size(); ++i) {
        const auto& x = ordered[i];
        const auto& y = ordered[i + 1];
        const auto& a = done.at(x).assigned;
        const auto& b = done.at(y).assigned;
        std::set<std::string> allowed;
        for (const auto& e : exceptions[y])
            if (e.wanted_from == x) allowed.insert(e.script);
        for (const auto& e : exceptions[x])
            if (e.wanted_from == y) allowed.insert(e.script);
        std::set<std::string> moved;
        for (const auto& [s, t] : a)
            if (b.count(s) && b.at(s) != t) moved.insert(s);
        for (const auto& s : moved)
            if (!allowed.count(s)) {
                problems.push_back("rule 2: " + x + "->" + y + " unrecorded " + s);
                break;
            }
        for (const auto& s : allowed)
            if (a.count(s) && b.count(s) && !moved.count(s)) {
                problems.push_back("rule 2: " + x + "->" + y + " recorded but not moved");
                break;
            }
    }
    return problems;
}

std::map<int, std::vector<std::string>> broken(const Schedule& schedule, int seeds, const LiveFn& live,
                                               const FillFn& fill) {
    std::map<int, std::vector<std::string>> out;
    for (int seed = 0; seed < seeds; ++seed) {
        auto p = run_history(seed, schedule, live, fill);
        if (!p.empty()) out[seed] = p;
    }
    return out;
}

const LiveFn kLive = numbering::allocate_live;
const FillFn kFill = numbering::allocate_fill;

bool mentions(const std::map<int, std::vector<std::string>>& failures, const std::string& rule) {
    for (const auto& [seed, problems] : failures)
        for (const auto& p : problems)
            if (util::starts_with(p, rule)) return true;
    return false;
}

}  // namespace

TEST_CASE("the contract holds across generated histories") {
    for (const auto& [name, schedule] : schedules()) {
        CAPTURE(name);
        auto failures = broken(schedule, 60, kLive, kFill);
        if (!failures.empty()) {
            CAPTURE(failures.begin()->first);
            CAPTURE(failures.begin()->second.front());
            CHECK(failures.empty());
        }
    }
}

// Mutation: prove the property test is not vacuous. Each re-introduces a defect the contract
// exists to catch, and the generated histories must catch it.
TEST_CASE("the property test catches unrecorded breaks") {
    FillFn silent = [](const std::string& date, const std::vector<std::string>& scripts, const DayAlloc* own,
                       const DayAlloc* earlier, const DayAlloc* later, const Holdings* state, Sequence& seq,
                       std::int64_t vid) {
        auto plan = numbering::allocate_fill(date, scripts, own, earlier, later, state, seq, vid);
        plan.exceptions.clear();
        return plan;
    };
    LiveFn silent_live = [](const std::string& date, const std::vector<std::string>& scripts, const Holdings* state,
                            const DayAlloc* previous, Sequence& seq, std::int64_t vid) {
        auto plan = numbering::allocate_live(date, scripts, state, previous, seq, vid);
        plan.exceptions.clear();
        return plan;
    };
    auto failures = broken(schedules().at("gaps filled newest-first"), 60, silent_live, silent);
    CHECK(mentions(failures, "rule 2"));
}

TEST_CASE("the property test catches a fill that overwrites a holding") {
    // The bug 7.x's simulation found, put back: a fill that records every step-4 number as a
    // holding, overwriting the one the script already had.
    FillFn overwriting = [](const std::string& date, const std::vector<std::string>& scripts, const DayAlloc* own,
                            const DayAlloc* earlier, const DayAlloc* later, const Holdings* state, Sequence& seq,
                            std::int64_t vid) {
        auto plan = numbering::allocate_fill(date, scripts, own, earlier, later, state, seq, vid);
        std::set<std::int64_t> retired(plan.retired.begin(), plan.retired.end());
        for (const auto& [s, t] : plan.day.assigned)
            if (plan.state_after.assigned.count(s) && retired.count(t)) plan.state_after.assigned[s] = t;
        return plan;
    };
    std::map<int, std::vector<std::string>> failures;
    for (const char* name : {"gaps filled oldest-first", "gaps filled newest-first", "fills interleaved with live runs"})
        for (auto& [seed, p] : broken(schedules().at(name), 200, kLive, overwriting)) failures[seed] = p;
    CHECK(mentions(failures, "rule 5"));
}

// -- golden replay: exact agreement with 7.0.0 --------------------------------------------
//
// tests/data/numbering_golden.json was recorded by running the 7.0.0 Python allocators over
// its own property-test schedules (two seeds each, Python's random): every step's input
// scripts and every output -- the day, the state after, exceptions, retired numbers, counts,
// neighbours and the counter either side. Replaying the same inputs through the C++
// allocators must reproduce all of it, which is the strongest evidence the port numbers the
// way 7.x did. Regenerate with scratchpad's gen_numbering_golden.py against releases/7.0.0.

namespace {

TokenMap token_map(const jsonio::Json& j) {
    TokenMap m;
    for (auto it = j.begin(); it != j.end(); ++it) m[it.key()] = it.value().get<std::int64_t>();
    return m;
}

std::vector<std::int64_t> ints(const jsonio::Json& j) {
    std::vector<std::int64_t> v;
    for (const auto& x : j) v.push_back(x.get<std::int64_t>());
    return v;
}

}  // namespace

TEST_CASE("golden: the C++ allocators reproduce every 7.0.0 decision") {
    auto doc = jsonio::read(std::string(PREMARKET_SOURCE_DIR) + "/tests/data/numbering_golden.json");
    REQUIRE(doc.size() == 14);
    for (const auto& history : doc) {
        CAPTURE(history["schedule"].get<std::string>());
        CAPTURE(history["seed"].get<int>());
        Sequence sequence;
        std::optional<Holdings> state;
        std::map<std::string, DayAlloc> done;
        for (const auto& step : history["steps"]) {
            std::string op = step["op"], date = step["date"];
            CAPTURE(op);
            CAPTURE(date);
            std::vector<std::string> scripts = step["scripts"].get<std::vector<std::string>>();
            Plan plan;
            if (op == "live" || op == "switch") {
                std::optional<std::string> previous;
                for (const auto& [x, a] : done)
                    if (x < date) previous = x;
                plan = numbering::allocate_live(date, scripts, state ? &*state : nullptr,
                                                previous ? &done.at(*previous) : nullptr, sequence, 1);
            } else {
                std::vector<std::string> numbered;
                for (const auto& [x, a] : done)
                    if (x != date) numbered.push_back(x);
                auto [earlier, later] = numbering::neighbours(date, numbered);
                auto get = [&](const std::optional<std::string>& k) -> const DayAlloc* {
                    return k && done.count(*k) ? &done.at(*k) : nullptr;
                };
                plan = numbering::allocate_fill(date, scripts, get(date), get(earlier), get(later),
                                                state ? &*state : nullptr, sequence, 1);
            }
            REQUIRE(plan.day.assigned == token_map(step["day"]["assigned"]));
            REQUIRE(plan.day.retained == token_map(step["day"]["retained"]));
            REQUIRE(plan.day.free == ints(step["day"]["free"]));
            REQUIRE(plan.state_after.assigned == token_map(step["state_after"]["assigned"]));
            REQUIRE(plan.state_after.free == ints(step["state_after"]["free"]));
            REQUIRE(plan.state_after.last_date ==
                    step["state_after"]["last_date"].get<std::map<std::string, std::string>>());
            REQUIRE(plan.retired == ints(step["retired"]));
            REQUIRE(plan.counts == step["counts"].get<std::map<std::string, std::int64_t>>());
            REQUIRE(plan.neighbours == step["neighbours"].get<std::map<std::string, std::string>>());
            REQUIRE(plan.counter_before == step["counter"][0].get<std::int64_t>());
            REQUIRE(plan.counter_after == step["counter"][1].get<std::int64_t>());
            REQUIRE(plan.exceptions.size() == step["exceptions"].size());
            for (std::size_t i = 0; i < plan.exceptions.size(); ++i) {
                const auto& e = plan.exceptions[i];
                const auto& g = step["exceptions"][i];
                REQUIRE(e.script == g["script"].get<std::string>());
                REQUIRE(e.token == g["token"].get<std::int64_t>());
                REQUIRE(e.wanted == g["wanted"].get<std::int64_t>());
                REQUIRE(e.wanted_from == g["wanted_from"].get<std::string>());
                REQUIRE(e.lost_to == g["lost_to"].get<std::string>());
            }
            state = plan.state_after;
            done[date] = plan.day;
        }
    }
}
