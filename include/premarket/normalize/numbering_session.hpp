#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/numbering.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/runner.hpp"

namespace premarket::numbering_session {

using numbering::DayAlloc;
using numbering::Holdings;
using numbering::Plan;

struct NumberingRefused : ValueError {
    using ValueError::ValueError;
};

struct Outcome {
    std::string date;
    std::string mic;
    std::string status;
    std::string detail;
    jsonio::Json as_dict() const;
};

bool within_window(const std::string& earlier, const std::string& later);
std::string exception_name(const std::exception& e);

class Session;

class VenueNumbering {
public:
    VenueNumbering(Session& session, std::string date, std::string mic, std::int64_t venue_id, std::string mode,
                   std::optional<Holdings> holdings, std::optional<DayAlloc> previous = std::nullopt,
                   std::optional<DayAlloc> own = std::nullopt, std::optional<DayAlloc> earlier = std::nullopt,
                   std::optional<DayAlloc> later = std::nullopt);

    bool preview() const;
    std::string describe() const;
    Plan plan(const std::vector<std::string>& scripts);
    void reserve(const Plan& plan);
    std::string summary(const Plan& plan) const;
    void commit(const Plan& plan, const std::string& started_at = "",
                const std::vector<counter_token::Artifact>& inputs = {},
                const std::vector<counter_token::Artifact>& outputs = {});

    const std::string& date() const { return date_; }
    const std::string& mic() const { return mic_; }
    const std::string& mode() const { return mode_; }

private:
    Session& session_;
    std::string date_, mic_;
    std::int64_t venue_id_;
    std::string mode_;
    std::optional<Holdings> holdings_;
    std::optional<DayAlloc> previous_, own_, earlier_, later_;
};

class Session {
public:
    Session(std::string command, std::string mode, std::string reason = "", bool preview = false,
            std::string label = "", state::LogFn log = nullptr);
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;

    void enter();
    void exit(const std::string* error);

    bool plans_preview() const { return preview && mode == numbering::FILL; }
    void fail(const std::string& error);
    void record(const std::string& date, const std::string& mic, const std::string& status,
                const std::string& detail = "");
    void no_input(const std::string& date, const std::string& mic, const std::string& detail = "");

    std::optional<Holdings> holdings(const std::string& mic);
    std::vector<std::string> days(const std::string& mic);
    std::optional<DayAlloc> day(const std::optional<std::string>& date, const std::string& mic);
    std::string mode_of(const std::string& date, const std::string& mic);
    std::string live_date(const std::string& mic);
    const state::VenueHead* venue_head(const std::string& mic) const;
    VenueNumbering venue(const std::string& date, const std::string& mic, std::int64_t venue_id);

    std::string command, mode, reason;
    bool preview;
    std::string label;
    state::LogFn log;
    std::vector<Outcome> outcomes;
    std::optional<state::Head> head;
    counter_token::Sequence sequence;
    std::string counter_source;
    std::string run_id;
    std::string backup_name;

private:
    friend class VenueNumbering;
    std::unique_ptr<state::Lock> lock_;
    bool entered_ = false;
    std::map<std::pair<std::string, std::string>, std::pair<DayAlloc, std::string>> days_;
    std::map<std::string, Holdings> holdings_;
    std::map<std::string, std::map<std::string, std::string>> marks_;
    jsonio::Json outcomes_json() const;
};

Session& of(runner::Opts& opts);

template <typename Fn>
auto with_session(Session& session, Fn&& fn) -> decltype(fn()) {
    session.enter();
    try {
        auto rc = fn();
        session.exit(nullptr);
        return rc;
    } catch (const std::exception& e) {
        std::string error = exception_name(e) + ": " + e.what();
        session.exit(&error);
        throw;
    } catch (const SystemExit& e) {
        std::string error = "SystemExit: " + e.message;
        session.exit(&error);
        throw;
    }
}

}  // namespace premarket::numbering_session
