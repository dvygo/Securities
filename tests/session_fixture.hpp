#pragma once
// A tree with 20260925's XNSE numbered 1..3 (the way the box looked when the numbering
// session was written), and helpers that drive a session the way a normalizer does.

#include <optional>
#include <string>
#include <vector>

#include "fixture.hpp"
#include "premarket/normalize/numbering_session.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"

namespace session_fixture {

using namespace premarket;
using numbering_session::Session;

struct Box : fixture::Tree {
    Box() {
        counter_token::write_sequence("20260925", counter_token::Sequence(3));
        counter_token::write_venue_manifest("20260925", "XNSE", {16, {{"N1", 1}, {"N2", 2}, {"N3", 3}}, {}, {}});
    }
};

inline state::Head init(const std::string& reason = "test") { return *state::init_state(reason, false, fixture::quiet); }

struct Numbered {
    numbering::Plan plan;
    std::vector<numbering_session::Outcome> outcomes;
};

inline Numbered number(const std::string& mode, const std::string& date, const std::string& mic,
                       std::int64_t venue_id, const std::vector<std::string>& scripts,
                       const std::string& reason = "test") {
    Session session("normalize", mode, reason, false, "", fixture::quiet);
    std::optional<numbering::Plan> plan;
    numbering_session::with_session(session, [&] {
        auto venue = session.venue(date, mic, venue_id);
        plan = venue.plan(scripts);
        venue.reserve(*plan);
        venue.commit(*plan, counter_token::utc_now());
        return 0;
    });
    return {*plan, session.outcomes};
}

template <typename Fn>
void in_session(const std::string& mode, Fn&& fn, const std::string& reason = "", bool preview = false) {
    Session session("normalize", mode, reason, preview, "", fixture::quiet);
    numbering_session::with_session(session, [&] {
        fn(session);
        return 0;
    });
}

inline std::vector<std::string> event_types(const std::string& job = "") {
    std::vector<std::string> out;
    for (const auto& e : state::read_events())
        if (job.empty() || e["job"]["name"] == job) out.push_back(e["eventType"].get<std::string>());
    return out;
}

inline std::size_t backups() { return fixture::files_ending(state::backups_dir(), ".tar.gz").size(); }

inline counter_token::TokenMap assigned(const std::string& date, const std::string& mic) {
    auto e = counter_token::venue_entry(date, mic);
    return e ? e->assigned : counter_token::TokenMap{};
}

inline void write_normalized(const std::string& date, const std::string& mic, const counter_token::TokenMap& tokens) {
    std::vector<parquet::Row> rows;
    std::int64_t n = 0;
    for (const auto& [script, token] : tokens) rows.push_back({script, std::to_string(++n), std::to_string(token)});
    fs::create_directories(paths::normalized_dir(date));
    parquet::write_rows(paths::normalized_dir(date) / (mic + "-DATABENTO-normalized.parquet"),
                        {"script", "counterToken", "counterTokenV2"}, rows);
}

inline numbering::Plan numbered(const std::string& mode, const std::string& date, const std::string& mic,
                                std::int64_t venue_id, const std::vector<std::string>& scripts,
                                const std::string& reason = "test") {
    auto run = number(mode, date, mic, venue_id, scripts, reason);
    write_normalized(date, mic, run.plan.day.assigned);
    return run.plan;
}

}  // namespace session_fixture
