#include "premarket/normalize/numbering_session.hpp"

#include <algorithm>
#include <set>
#include <system_error>

#include "premarket/log.hpp"
#include "premarket/paths.hpp"

namespace premarket::numbering_session {

using jsonio::Json;

Json Outcome::as_dict() const { return {{"date", date}, {"mic", mic}, {"status", status}, {"detail", detail}}; }

bool within_window(const std::string& earlier, const std::string& later) {
    return util::days_between(earlier, later) <= counter_token::MANIFEST_LOOKBACK_DAYS;
}

std::string exception_name(const std::exception& e) {
    if (dynamic_cast<const NumberingRefused*>(&e)) return "NumberingRefused";
    if (dynamic_cast<const state::StateCorrupt*>(&e)) return "StateCorrupt";
    if (dynamic_cast<const state::StateMissing*>(&e)) return "StateMissing";
    if (dynamic_cast<const state::LockHeld*>(&e)) return "LockHeld";
    if (dynamic_cast<const counter_token::ManifestCorrupt*>(&e)) return "ManifestCorrupt";
    if (dynamic_cast<const FileNotFoundError*>(&e)) return "FileNotFoundError";
    if (dynamic_cast<const ValueError*>(&e)) return "ValueError";
    if (dynamic_cast<const RuntimeError*>(&e)) return "RuntimeError";
    return "Exception";
}

Session::Session(std::string command_, std::string mode_, std::string reason_, bool preview_, std::string label_,
                 state::LogFn log_)
    : command(std::move(command_)),
      mode(std::move(mode_)),
      reason(std::move(reason_)),
      preview(preview_),
      label(label_.empty() ? mode : std::move(label_)),
      log(log_ ? std::move(log_) : state::LogFn([](const std::string& s) { log::out(s); })) {}

Session::~Session() = default;

Json Session::outcomes_json() const {
    Json out = Json::array();
    for (const auto& o : outcomes) out.push_back(o.as_dict());
    return out;
}

void Session::enter() {
    lock_ = std::make_unique<state::Lock>(command);
    try {
        head = state::require_head();
        if (!preview) {
            state::recover(*head, log);
            head = state::require_head();
        }
        auto [issued, source] = state::global_counter(&*head);
        counter_source = source;
        sequence = counter_token::Sequence(issued);
        run_id = state::new_run_id(label, mode);
        if (!preview) {
            Json facets = {{"command", command},
                           {"mode", mode},
                           {"reason", reason},
                           {"operator", state::operator_id()},
                           {"counter_before", issued}};
            if (mode == numbering::FILL) {
                auto [archive, pruned] = state::backup(run_id);
                backup_name = archive.filename().string();
                Json p = Json::array();
                for (const auto& x : pruned) p.push_back(x);
                facets["backup"] = backup_name;
                facets["backups_pruned"] = p;
                log("  backup: " + archive.string());
            }
            state::append_event(state::event("START", run_id, command, facets));
        }
    } catch (...) {
        lock_.reset();
        throw;
    }
    entered_ = true;
}

void Session::exit(const std::string* error) {
    if (!entered_) return;
    entered_ = false;
    try {
        if (!preview && !run_id.empty()) {
            Json facets = {{"counter_after", sequence.issued()}, {"outcomes", outcomes_json()}};
            if (error) facets["error"] = *error;
            state::append_event(state::event(error ? "FAIL" : "COMPLETE", run_id, command, facets));
        }
    } catch (...) {
        lock_.reset();
        throw;
    }
    lock_.reset();
}

void Session::fail(const std::string& error) {
    if (!preview && !run_id.empty()) {
        state::append_event(state::event(
            "FAIL", run_id, command,
            {{"error", error}, {"counter_after", sequence.issued()}, {"outcomes", outcomes_json()}}));
        run_id.clear();
    }
}

void Session::record(const std::string& date, const std::string& mic, const std::string& status,
                     const std::string& detail) {
    outcomes.push_back({date, util::upper(mic), status, detail});
}

void Session::no_input(const std::string& date, const std::string& mic, const std::string& detail) {
    record(date, mic, "no-input", detail.empty() ? "no input files for this date" : detail);
}

const state::VenueHead* Session::venue_head(const std::string& mic) const {
    if (!head) return nullptr;
    auto it = head->venues.find(mic);
    return it == head->venues.end() ? nullptr : &it->second;
}

std::optional<Holdings> Session::holdings(const std::string& mic) {
    if (auto it = holdings_.find(mic); it != holdings_.end()) return it->second.copy();
    const auto* vh = venue_head(mic);
    if (vh == nullptr) return std::nullopt;
    return state::load_snapshot(mic, *vh);
}

std::vector<std::string> Session::days(const std::string& mic) {
    std::set<std::string> all;
    for (const auto& d : state::venue_days(mic)) all.insert(d);
    for (const auto& [key, value] : days_)
        if (key.second == mic) all.insert(key.first);
    return {all.begin(), all.end()};
}

std::optional<DayAlloc> Session::day(const std::optional<std::string>& date, const std::string& mic) {
    if (!date) return std::nullopt;
    if (auto it = days_.find({*date, mic}); it != days_.end()) return it->second.first;
    return state::day_alloc(*date, mic);
}

std::string Session::mode_of(const std::string& date, const std::string& mic) {
    if (auto it = days_.find({date, mic}); it != days_.end()) return it->second.second;
    Json header = counter_token::read_json(counter_token::venue_manifest_path(date, mic));
    const Json& m = counter_token::jget(counter_token::jget(header, "numbering"), "mode");
    return m.is_null() ? std::string(numbering::LIVE) : counter_token::jstr(m);
}

std::string Session::live_date(const std::string& mic) {
    if (auto it = marks_.find(mic); it != marks_.end()) {
        auto ld = it->second.find("live_date");
        return ld == it->second.end() ? "" : ld->second;
    }
    const auto* vh = venue_head(mic);
    return vh ? vh->live_date : "";
}

VenueNumbering Session::venue(const std::string& date, const std::string& mic_in, std::int64_t venue_id) {
    std::string mic = util::upper(mic_in);
    auto held = holdings(mic);
    if (held && held->venue_id != venue_id)
        throw NumberingRefused(mic + ": venue_id is " + std::to_string(venue_id) + " in config.ini but " +
                               std::to_string(held->venue_id) +
                               " in the state. Refusing to renumber a venue under a different id.");
    auto known = days(mic);
    if (mode == numbering::LIVE) {
        if (!held && !known.empty())
            throw NumberingRefused(mic + ": has numbered days on disk (" + known.back() +
                                   " newest) but no state. It was numbered outside a numbering session; refusing to "
                                   "start it from scratch.");
        std::optional<std::string> newest;
        if (!known.empty()) newest = known.back();
        if (newest && date < *newest)
            throw NumberingRefused(mic + ": " + date + " is older than " + *newest +
                                   ", which is already numbered. Numbering it as a live day would fork the chain; fill "
                                   "it with `normalize --dates=" + date + " --venue " + mic + " --reason ...`.");
        if (newest && *newest == date && mode_of(date, mic) == numbering::FILL)
            throw NumberingRefused(mic + ": " + date +
                                   " was filled from its neighbours; it cannot be re-numbered as a live day. Run live "
                                   "on a later date.");
        std::optional<std::string> previous;
        for (const auto& d : known)
            if (d < date && (!previous || d > *previous)) previous = d;
        return VenueNumbering(*this, date, mic, venue_id, numbering::LIVE, held, day(previous, mic));
    }
    std::string ld = live_date(mic);
    if (!ld.empty() && date >= ld)
        throw NumberingRefused(mic + ": " + date + " is not older than " + ld +
                               ", the newest live day. --dates fills older days; number it live with --date-dir.");
    std::optional<DayAlloc> own;
    if (std::find(known.begin(), known.end(), date) != known.end()) own = day(date, mic);
    std::vector<std::string> others;
    for (const auto& d : known)
        if (d != date) others.push_back(d);
    auto [earlier, later] = numbering::neighbours(date, others);
    if (earlier && !within_window(*earlier, date)) earlier.reset();
    if (later && !within_window(date, *later)) later.reset();
    return VenueNumbering(*this, date, mic, venue_id, numbering::FILL, held, std::nullopt, own, day(earlier, mic),
                          day(later, mic));
}

Session& of(runner::Opts& opts) {
    if (opts.numbering == nullptr)
        throw RuntimeError(
            "counterTokenV2 is only assigned inside a numbering session (`premarket normalize` / `plugin` open one); "
            "refusing to number outside it");
    return *opts.numbering;
}

VenueNumbering::VenueNumbering(Session& session, std::string date, std::string mic, std::int64_t venue_id,
                               std::string mode, std::optional<Holdings> holdings, std::optional<DayAlloc> previous,
                               std::optional<DayAlloc> own, std::optional<DayAlloc> earlier,
                               std::optional<DayAlloc> later)
    : session_(session),
      date_(std::move(date)),
      mic_(std::move(mic)),
      venue_id_(venue_id),
      mode_(std::move(mode)),
      holdings_(std::move(holdings)),
      previous_(std::move(previous)),
      own_(std::move(own)),
      earlier_(std::move(earlier)),
      later_(std::move(later)) {}

bool VenueNumbering::preview() const { return session_.preview; }

std::string VenueNumbering::describe() const {
    if (mode_ == numbering::LIVE)
        return std::string(holdings_ ? "continuing the state" : "first day") +
               (previous_ ? ", previous numbered day " + previous_->date : "");
    std::vector<std::string> parts;
    if (own_) parts.push_back("re-run of its own fill");
    if (later_) parts.push_back("later " + later_->date);
    if (earlier_) parts.push_back("earlier " + earlier_->date);
    return "filled from " + (parts.empty() ? std::string("nothing -- every number new") : util::join(parts, ", "));
}

Plan VenueNumbering::plan(const std::vector<std::string>& scripts) {
    auto& seq = session_.sequence;
    if (mode_ == numbering::LIVE)
        return numbering::allocate_live(date_, scripts, holdings_ ? &*holdings_ : nullptr,
                                        previous_ ? &*previous_ : nullptr, seq, venue_id_);
    return numbering::allocate_fill(date_, scripts, own_ ? &*own_ : nullptr, earlier_ ? &*earlier_ : nullptr,
                                    later_ ? &*later_ : nullptr, holdings_ ? &*holdings_ : nullptr, seq, venue_id_);
}

void VenueNumbering::reserve(const Plan&) {
    if (preview()) return;
    std::int64_t existing = counter_token::load_sequence(date_).value_or(0);
    if (session_.sequence.issued() > existing) counter_token::write_sequence(date_, session_.sequence);
}

std::string VenueNumbering::summary(const Plan& plan) const {
    auto c = [&](const char* k) {
        auto it = plan.counts.find(k);
        return util::commas(it == plan.counts.end() ? 0 : it->second);
    };
    return c("scripts") + " scripts, " + c("drawn") + " new number(s), " + c("exceptions") + " exception(s), " +
           c("retained") + " retained, counter " + util::commas(plan.counter_before) + " -> " +
           util::commas(plan.counter_after);
}

namespace {

Json counts_json(const std::map<std::string, std::int64_t>& counts) {
    Json out = Json::object();
    for (const auto& [k, v] : counts) out[k] = v;
    return out;
}

Json strings_json(const std::map<std::string, std::string>& m) {
    Json out = Json::object();
    for (const auto& [k, v] : m) out[k] = v;
    return out;
}

}  // namespace

void VenueNumbering::commit(const Plan& plan, const std::string& started_at,
                            const std::vector<counter_token::Artifact>& inputs,
                            const std::vector<counter_token::Artifact>& outputs) {
    Session& session = session_;
    if (preview()) {
        session.days_[{date_, mic_}] = {plan.day, mode_};
        session.holdings_[mic_] = plan.state_after.copy();
        if (!session.marks_.count(mic_)) session.marks_[mic_] = {{"live_date", session.live_date(mic_)}};
        auto& marks = session.marks_[mic_];
        if (mode_ == numbering::LIVE) marks["live_date"] = std::max(marks["live_date"], date_);
        session.record(date_, mic_, "previewed", summary(plan));
        session.log("    PREVIEW " + date_ + " " + mic_ + " [" + mode_ + "] " + describe() + ": " + summary(plan));
        return;
    }

    auto root = paths::data_root();
    auto rel = [&](const fs::path& p) { return util::relative_to(p, root); };
    std::string run_id = state::new_run_id(date_, mode_);
    counter_token::VenueTokens tokens;
    tokens.venue_id = venue_id_;
    tokens.assigned = plan.day.assigned;
    tokens.free = plan.day.free;
    tokens.retained = plan.day.retained;
    Json staged = Json::array();

    auto alloc_final = counter_token::alloc_path(date_, mic_);
    fs::path alloc_staged = alloc_final.string() + state::STAGED;
    counter_token::write_alloc(alloc_staged, tokens);
    std::string alloc_sha = counter_token::sha256_of(alloc_staged);
    auto alloc_bytes = static_cast<std::int64_t>(fs::file_size(alloc_staged));
    staged.push_back({{"staged", rel(alloc_staged)}, {"final", rel(alloc_final)}, {"sha256", alloc_sha}});

    Json exceptions_block = {{"path", ""}, {"sha256", ""}, {"count", 0}};
    auto exceptions_final = counter_token::exceptions_path(date_, mic_);
    if (!plan.exceptions.empty()) {
        fs::path exceptions_staged = exceptions_final.string() + state::STAGED;
        std::vector<counter_token::ExceptionRow> rows;
        for (const auto& e : plan.exceptions) rows.push_back(e.as_row());
        counter_token::write_exceptions(exceptions_staged, rows);
        std::string digest = counter_token::sha256_of(exceptions_staged);
        staged.push_back({{"staged", rel(exceptions_staged)}, {"final", rel(exceptions_final)}, {"sha256", digest}});
        exceptions_block = {{"path", exceptions_final.filename().string()},
                            {"sha256", digest},
                            {"count", static_cast<std::int64_t>(plan.exceptions.size())}};
    }

    const state::VenueHead* vh = session.venue_head(mic_);
    std::optional<state::VenueHead> vh_copy;
    if (vh) vh_copy = *vh;
    auto snap = state::write_snapshot(mic_, run_id, plan.state_after, vh_copy ? &*vh_copy : nullptr, true);
    if (!vh_copy || snap.relative != vh_copy->snapshot) {
        auto final_path = state::snapshot_path(snap.relative);
        staged.push_back(
            {{"staged", rel(final_path) + state::STAGED}, {"final", rel(final_path)}, {"sha256", snap.sha256}});
    }

    Json numbering_block = {
        {"mode", mode_},
        {"run_id", run_id},
        {"session", session.run_id},
        {"neighbours", strings_json(plan.neighbours)},
        {"state_before",
         {{"snapshot", vh_copy ? vh_copy->snapshot : ""}, {"sha256", vh_copy ? vh_copy->sha256 : ""}}},
        {"state_after", {{"snapshot", snap.relative}, {"sha256", snap.sha256}}},
        {"counter_before", plan.counter_before},
        {"counter_after", plan.counter_after},
        {"counts", counts_json(plan.counts)},
        {"retired", static_cast<std::int64_t>(plan.retired.size())},
        {"exceptions", exceptions_block},
        {"reason", session.reason},
        {"operator", state::operator_id()},
    };
    std::string carried;
    if (auto l = plan.neighbours.find("later"); l != plan.neighbours.end() && !l->second.empty()) carried = l->second;
    else if (auto e = plan.neighbours.find("earlier"); e != plan.neighbours.end()) carried = e->second;
    counter_token::RunStats run;
    run.day = plan.counts;
    run.carried_from = carried;
    run.drawn = plan.counter_after - plan.counter_before;
    run.sequence_before = plan.counter_before;
    run.sequence_after = plan.counter_after;
    run.anchored_on = mode_ == numbering::LIVE ? std::string("state") : (own_ ? date_ : carried);
    run.sequence_from = session.counter_source;
    Json payload = counter_token::header_payload(date_, mic_, tokens, alloc_final.filename().string(), alloc_bytes,
                                                 alloc_sha, started_at, &run, inputs, outputs, &numbering_block);

    auto& head = *session.head;
    std::string newest = vh_copy ? vh_copy->newest : "";
    std::string newest_mode = vh_copy ? vh_copy->newest_mode : "";
    if (date_ >= newest) {
        newest = date_;
        newest_mode = mode_;
    }
    std::string live_date = vh_copy ? vh_copy->live_date : "";
    if (mode_ == numbering::LIVE) live_date = std::max(live_date, date_);
    head.counter = std::max(head.counter, session.sequence.issued());
    state::VenueHead next;
    next.venue_id = venue_id_;
    next.snapshot = snap.relative;
    next.sha256 = snap.sha256;
    next.rows = snap.rows;
    next.newest = newest;
    next.newest_mode = newest_mode;
    next.live_date = live_date;
    next.run_id = run_id;
    next.updated_at = counter_token::utc_now();
    head.venues[mic_] = next;
    head.last_run = {{"run_id", run_id},       {"session", session.run_id},   {"command", session.command},
                     {"date", date_},          {"venue", mic_},               {"mode", mode_},
                     {"reason", session.reason}, {"operator", state::operator_id()},
                     {"completed_at", counter_token::utc_now()}};
    head.last_commit = {{"run_id", run_id}, {"date", date_}, {"mic", mic_}, {"staged", staged}, {"header", payload}};
    state::write_head(head);

    for (const auto& item : staged) {
        util::failpoint("promote");
        util::replace_file(root / counter_token::jstr(item["staged"]), root / counter_token::jstr(item["final"]));
    }
    std::error_code ec;
    if (plan.exceptions.empty() && fs::exists(exceptions_final, ec)) fs::remove(exceptions_final);
    counter_token::write_header(date_, mic_, payload);

    Json facets = numbering_block;
    facets["date"] = date_;
    facets["venue"] = mic_;
    Json retired = Json::array();
    for (std::size_t i = 0; i < plan.retired.size() && i < 1000; ++i) retired.push_back(plan.retired[i]);
    facets["retired_tokens"] = retired;
    Json in = Json::array(), out = Json::array();
    for (const auto& a : inputs) in.push_back(a.as_dict());
    for (const auto& a : outputs) out.push_back(a.as_dict());
    state::append_event(state::event("COMPLETE", run_id, session.command, facets, in, out, session.run_id));
    session.record(date_, mic_, "done", summary(plan));
}

}  // namespace premarket::numbering_session
