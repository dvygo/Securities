#include "premarket/normalize/state.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <set>
#include <system_error>

#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/targz.hpp"

namespace premarket::state {

using counter_token::jget;
using counter_token::jint;
using counter_token::jstr;
using counter_token::truthy;

fs::path state_dir() { return paths::data_root() / STATE_DIR; }
fs::path head_path() { return state_dir() / HEAD_NAME; }
fs::path snapshot_path(const std::string& relative) { return state_dir() / relative; }
fs::path runs_path() { return state_dir() / RUNS_NAME; }
fs::path backups_dir() { return state_dir() / "backups"; }
std::string backups_hint() { return (state_dir() / "backups").string(); }

std::int64_t VenueHead::venue_id_int() const { return jint(venue_id); }
std::int64_t VenueHead::rows_int() const { return jint(rows); }

Json VenueHead::as_dict() const {
    return {{"venue_id", venue_id}, {"snapshot", snapshot},     {"sha256", sha256},
            {"rows", rows},         {"newest", newest},         {"newest_mode", newest_mode},
            {"live_date", live_date}, {"run_id", run_id},       {"updated_at", updated_at}};
}

VenueHead VenueHead::from_dict(const Json& doc) {
    static const std::set<std::string> known = {"venue_id", "snapshot", "sha256", "rows", "newest",
                                                "newest_mode", "live_date", "run_id", "updated_at"};
    if (!doc.is_object()) throw ValueError("VenueHead() argument after ** must be a mapping");
    for (auto it = doc.begin(); it != doc.end(); ++it)
        if (!known.count(it.key()))
            throw ValueError("VenueHead.__init__() got an unexpected keyword argument '" + it.key() + "'");
    for (const char* required : {"venue_id", "snapshot", "sha256", "rows"})
        if (!doc.contains(required))
            throw ValueError(std::string("VenueHead.__init__() missing required argument: '") + required + "'");
    VenueHead v;
    v.venue_id = doc["venue_id"];
    v.snapshot = jstr(doc["snapshot"]);
    v.sha256 = jstr(doc["sha256"]);
    v.rows = doc["rows"];
    for (auto [key, field] : std::initializer_list<std::pair<const char*, std::string*>>{
             {"newest", &v.newest}, {"newest_mode", &v.newest_mode}, {"live_date", &v.live_date},
             {"run_id", &v.run_id}, {"updated_at", &v.updated_at}})
        if (doc.contains(key)) *field = jstr(doc[key]);
    return v;
}

Json Head::as_dict() const {
    Json venues_doc = Json::object();
    for (const auto& [mic, vh] : venues) venues_doc[mic] = vh.as_dict();
    return {{"counter", counter},
            {"updated_at", updated_at},
            {"last_run", last_run},
            {"venues", venues_doc},
            {"last_commit", last_commit},
            {"version", version}};
}

Head Head::from_dict(const Json& doc) {
    if (!doc.is_object()) throw ValueError("head is not a JSON object");
    Head h;
    const Json& venues = jget(doc, "venues");
    if (truthy(venues) && !venues.is_object()) throw ValueError("'venues' is not a mapping");
    if (venues.is_object())
        for (auto it = venues.begin(); it != venues.end(); ++it) h.venues[it.key()] = VenueHead::from_dict(it.value());
    h.counter = jint(jget(doc, "counter"), 0);
    h.updated_at = doc.contains("updated_at") ? jstr(doc["updated_at"]) : "";
    h.last_run = truthy(jget(doc, "last_run")) ? doc["last_run"] : Json::object();
    h.last_commit = doc.contains("last_commit") ? doc["last_commit"] : Json();
    h.version = static_cast<int>(doc.contains("version") ? jint(doc["version"]) : HEAD_VERSION);
    return h;
}

std::optional<Head> read_head() {
    auto path = head_path();
    std::error_code ec;
    if (!fs::exists(path, ec)) return std::nullopt;
    Head head;
    try {
        head = Head::from_dict(jsonio::read(path));
    } catch (const std::exception& exc) {
        throw StateCorrupt(path.string() + " is unreadable (" + exc.what() +
                           "). Refusing to number anything against a state that cannot be read; restore it from " +
                           backups_hint());
    }
    if (head.version < HEAD_VERSION)
        throw StateCorrupt(path.string() + " is a 7.x numbering state (head version " + std::to_string(head.version) +
                           "). Run `premarket migrate-state --reason \"...\"` once to move this data tree to v8.");
    if (head.version != HEAD_VERSION)
        throw StateCorrupt(path.string() + " is version " + std::to_string(head.version) + "; this build reads version " +
                           std::to_string(HEAD_VERSION));
    return head;
}

Head require_head() {
    auto head = read_head();
    if (!head) {
        throw StateMissing("no state head at " + head_path().string() +
                           ". Numbering continues from the latest state, so it has to exist first: run `premarket "
                           "init-state --reason \"...\"` once on this host.");
    }
    return *head;
}

void atomic_write(const fs::path& path, const std::string& payload) {
    auto staging = util::staging_path(path);
    util::write_file_fsync(staging, payload);
    util::replace_file(staging, path);
    util::fsync_dir(path.parent_path());
}

fs::path write_head(Head& head) {
    auto path = head_path();
    fs::create_directories(path.parent_path());
    head.updated_at = counter_token::utc_now();
    atomic_write(path, jsonio::pretty(head.as_dict()));
    return path;
}

Snapshot write_snapshot(const std::string& mic_in, const std::string& run_id, const numbering::Holdings& holdings,
                        const VenueHead* current, bool staged) {
    std::string mic = util::upper(mic_in);
    std::string relative = mic + "/" + run_id + counter_token::ALLOC_SUFFIX;
    auto path = snapshot_path(relative);
    fs::create_directories(path.parent_path());
    std::set<std::int64_t> free(holdings.free.begin(), holdings.free.end());
    std::vector<parquet::NullableRow> rows;
    rows.reserve(holdings.assigned.size() + free.size());
    for (const auto& [s, t] : holdings.assigned) {
        auto ld = holdings.last_date.find(s);
        rows.push_back({s, std::to_string(t), std::string(counter_token::ALLOC_ASSIGNED),
                        ld == holdings.last_date.end() ? std::string() : ld->second});
    }
    for (auto t : free) rows.push_back({std::nullopt, std::to_string(t), std::string(counter_token::ALLOC_FREE), std::nullopt});
    auto staging = util::staging_path(path);
    parquet::write_table(staging,
                         {{"script", parquet::Type::String},
                          {"token", parquet::Type::Int32},
                          {"state", parquet::Type::String},
                          {"last_date", parquet::Type::String}},
                         rows);
    std::string digest = counter_token::sha256_of(staging);
    std::error_code ec;
    if (current != nullptr && current->sha256 == digest && fs::exists(snapshot_path(current->snapshot), ec)) {
        fs::remove(staging, ec);
        return {current->snapshot, current->sha256, current->rows_int()};
    }
    if (fs::exists(path, ec)) {
        fs::remove(staging, ec);
        throw StateCorrupt("snapshot " + path.string() +
                           " already exists; snapshots are never overwritten (run ids must be unique)");
    }
    util::replace_file(staging, staged ? fs::path(path.string() + STAGED) : path);
    return {relative, digest, static_cast<std::int64_t>(rows.size())};
}

numbering::Holdings load_snapshot(const std::string& mic, const VenueHead& venue) {
    auto path = snapshot_path(venue.snapshot);
    std::error_code ec;
    if (!fs::exists(path, ec))
        throw StateCorrupt(mic + ": the head names snapshot " + venue.snapshot +
                           ", which does not exist. Refusing to number against a state that cannot be read; restore "
                           "from " + backups_hint());
    std::string actual = counter_token::sha256_of(path);
    if (actual != venue.sha256)
        throw StateCorrupt(mic + ": snapshot " + venue.snapshot + " hashes to " + actual + ", the head records " +
                           venue.sha256 + ". It changed after it was written.");
    auto table = parquet::read_table(path, {"script", "token", "state", "last_date"});
    numbering::Holdings h;
    h.venue_id = venue.venue_id_int();
    for (const auto& row : table.rows) {
        const auto& script = row[0];
        const auto& token = row[1];
        const auto& st = row[2];
        const auto& last_date = row[3];
        bool has_script = script && !script->empty();
        if (st && *st == counter_token::ALLOC_ASSIGNED && has_script) {
            h.assigned[*script] = std::stoll(token.value_or("0"));
            h.last_date[*script] = last_date.value_or("");
        } else if (st && *st == counter_token::ALLOC_FREE && !has_script) {
            h.free.push_back(std::stoll(token.value_or("0")));
        } else {
            throw StateCorrupt(mic + ": snapshot " + venue.snapshot +
                               " has a row that is neither assigned-with-script nor free-without-one");
        }
    }
    std::sort(h.free.begin(), h.free.end());
    return h;
}

std::vector<std::string> venue_days(const std::string& mic_in) {
    std::string mic = util::upper(mic_in);
    std::vector<std::string> out;
    std::error_code ec;
    for (const auto& d : counter_token::numbered_day_dirs())
        if (fs::exists(counter_token::manifests_dir(d) / (mic + ".json"), ec)) out.push_back(d);
    return out;
}

std::optional<numbering::DayAlloc> day_alloc(const std::string& date, const std::string& mic) {
    auto entry = counter_token::venue_entry(date, mic);
    if (!entry) return std::nullopt;
    numbering::DayAlloc d;
    d.date = date;
    d.assigned = entry->assigned;
    d.retained = entry->retained;
    d.free = entry->free;
    std::sort(d.free.begin(), d.free.end());
    return d;
}

std::pair<std::int64_t, std::string> global_counter(const Head* head) {
    auto [scanned, where] = counter_token::highest_issued();
    std::int64_t recorded = head != nullptr ? head->counter : 0;
    if (recorded >= scanned) return {recorded, recorded ? HEAD_NAME : ""};
    return {scanned, where};
}

std::string new_run_id(const std::string& date, const std::string& mode) {
    static std::atomic<int> run_counter{1};
    return util::utc_stamp_micro() + "-" + date + "-" + mode + "-" + std::to_string(util::pid()) + "-" +
           std::to_string(run_counter++);
}

std::string operator_id() { return util::username() + "@" + util::hostname(); }

Lock::Lock(const std::string& command) {
    auto path = state_dir() / ".lock";
    fs::create_directories(path.parent_path());
    fd_ = ::open(path.c_str(), O_RDWR | O_CREAT | O_APPEND, 0644);
    if (fd_ < 0) throw RuntimeError("cannot open " + path.string());
    if (::flock(fd_, LOCK_EX | LOCK_NB) != 0) {
        std::string holder = util::strip(util::read_file(path));
        if (holder.empty()) holder = "unknown holder";
        ::close(fd_);
        fd_ = -1;
        throw LockHeld("another numbering run holds " + path.string() + ": " + holder +
                       ". Runs are serialised so two can never hand out the same number; wait for it to finish.");
    }
    jsonio::OrderedJson who;
    who["pid"] = util::pid();
    who["command"] = command;
    who["operator"] = operator_id();
    who["since"] = counter_token::utc_now();
    std::string line = jsonio::line(who);
    if (::ftruncate(fd_, 0) != 0 || ::write(fd_, line.data(), line.size()) < 0) {
        ::flock(fd_, LOCK_UN);
        ::close(fd_);
        fd_ = -1;
        throw RuntimeError("cannot write " + path.string());
    }
}

Lock::~Lock() {
    if (fd_ < 0) return;
    if (::ftruncate(fd_, 0) != 0) {
    }
    ::flock(fd_, LOCK_UN);
    ::close(fd_);
}

void append_event(const Json& ev) {
    auto path = runs_path();
    fs::create_directories(path.parent_path());
    std::string line = jsonio::line(ev) + "\n";
    int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) throw RuntimeError("cannot append to " + path.string());
    auto n = ::write(fd, line.data(), line.size());
    ::fsync(fd);
    ::close(fd);
    if (n != static_cast<ssize_t>(line.size())) throw RuntimeError("short write to " + path.string());
}

std::vector<Json> read_events() {
    std::vector<Json> out;
    auto path = runs_path();
    std::error_code ec;
    if (!fs::exists(path, ec)) return out;
    for (const auto& line : util::splitlines(util::read_file(path)))
        if (!util::strip(line).empty()) out.push_back(jsonio::parse(line));
    return out;
}

namespace {

Json lineage_files(const Json& artifacts) {
    Json out = Json::array();
    for (const auto& a : artifacts) {
        out.push_back({{"namespace", "file"},
                       {"name", a.at("path")},
                       {"facets",
                        {{"digest", {{"sha256", a.contains("sha256") ? a["sha256"] : Json("")}}},
                         {"rows", a.contains("rows") ? a["rows"] : Json(0)}}}});
    }
    return out;
}

}  // namespace

Json event(const std::string& event_type, const std::string& run_id, const std::string& job, const Json& facets,
           const Json& inputs, const Json& outputs, const std::string& parent) {
    Json run = {{"runId", run_id}, {"facets", {{"premarket", facets}}}};
    if (!parent.empty()) run["facets"]["parent"] = {{"run", {{"runId", parent}}}};
    return {{"eventType", event_type},
            {"eventTime", counter_token::utc_now()},
            {"producer", "premarket/" + counter_token::build_sha()},
            {"job", {{"namespace", "premarket"}, {"name", job}}},
            {"run", run},
            {"inputs", lineage_files(inputs)},
            {"outputs", lineage_files(outputs)}};
}

std::pair<fs::path, std::vector<std::string>> backup(const std::string& run_id) {
    auto root = paths::data_root();
    auto target = backups_dir() / (run_id + ".tar.gz");
    fs::create_directories(target.parent_path());
    auto staging = util::staging_path(target);
    {
        targz::Writer archive(staging);
        for (const auto& day : counter_token::numbered_day_dirs()) {
            auto dir = counter_token::manifests_dir(day);
            archive.add(dir, util::relative_to(dir, root));
        }
        std::error_code ec;
        if (fs::exists(head_path(), ec)) archive.add(head_path(), util::relative_to(head_path(), root));
        archive.close();
    }
    util::replace_file(staging, target);
    std::vector<fs::path> existing;
    for (const auto& p : util::list_dir_sorted(backups_dir()))
        if (util::ends_with(p.filename().string(), ".tar.gz")) existing.push_back(p);
    std::vector<std::string> pruned;
    if (existing.size() > static_cast<std::size_t>(BACKUPS_KEEP)) {
        for (std::size_t i = 0; i + BACKUPS_KEEP < existing.size(); ++i) {
            fs::remove(existing[i]);
            pruned.push_back(existing[i].filename().string());
        }
    }
    return {target, pruned};
}

bool path_less(const fs::path& a, const fs::path& b) {
    auto ia = a.begin(), ib = b.begin();
    for (; ia != a.end() && ib != b.end(); ++ia, ++ib) {
        auto sa = ia->string(), sb = ib->string();
        if (sa != sb) return sa < sb;
    }
    return ia == a.end() && ib != b.end();
}

namespace {

void glob_staged(const fs::path& dir, std::vector<fs::path>& out) {
    std::error_code ec;
    if (!fs::is_directory(dir, ec)) return;
    for (const auto& p : util::list_dir_sorted(dir))
        if (util::ends_with(p.filename().string(), STAGED)) out.push_back(p);
}

}  // namespace

std::vector<std::string> recover(const Head& head, const LogFn& log) {
    std::vector<std::string> actions;
    Json commit = head.last_commit.is_object() ? head.last_commit : Json::object();
    std::map<std::string, Json> wanted;
    for (const auto& item : jget(commit, "staged").is_array() ? commit["staged"] : Json::array())
        wanted[jstr(item.at("staged"))] = item;
    auto root = paths::data_root();
    std::vector<fs::path> staged;
    std::error_code ec;
    if (fs::is_directory(root, ec))
        for (const auto& child : util::list_dir_sorted(root))
            glob_staged(child / paths::TRANSFORM_DIR / "manifests", staged);
    if (fs::is_directory(state_dir(), ec))
        for (const auto& child : util::list_dir_sorted(state_dir())) glob_staged(child, staged);
    std::sort(staged.begin(), staged.end(), path_less);
    for (const auto& p : staged) {
        std::string relative = util::relative_to(p, root);
        auto it = wanted.find(relative);
        if (it == wanted.end()) {
            fs::remove(p);
            actions.push_back("deleted uncommitted " + relative);
            continue;
        }
        if (counter_token::sha256_of(p) != jstr(it->second.at("sha256")))
            throw StateCorrupt("recovery: " + relative +
                               " does not match the digest the head committed; refusing to promote it. Restore from " +
                               backups_hint());
        std::string final_rel = jstr(it->second.at("final"));
        util::replace_file(p, root / final_rel);
        actions.push_back("promoted " + final_rel);
    }
    if (truthy(jget(commit, "header"))) {
        std::string date = jstr(commit.at("date")), mic = jstr(commit.at("mic"));
        Json on_disk = counter_token::read_json(counter_token::venue_manifest_path(date, mic));
        const Json& run_id = jget(jget(on_disk, "numbering"), "run_id");
        if (run_id != commit.at("run_id")) {
            counter_token::write_header(date, mic, commit["header"]);
            actions.push_back("wrote the " + mic + " " + date + " header for " + jstr(commit["run_id"]));
        }
    }
    auto events = read_events();
    std::set<std::string> ended;
    for (const auto& e : events) {
        std::string type = jstr(e.at("eventType"));
        if (type == "COMPLETE" || type == "FAIL") ended.insert(jstr(e.at("run").at("runId")));
    }
    for (const auto& e : events) {
        std::string id = jstr(e.at("run").at("runId"));
        if (jstr(e.at("eventType")) == "START" && !ended.count(id)) {
            append_event(event("FAIL", id, jstr(e.at("job").at("name")),
                               {{"recovered", true}, {"error", "interrupted before it finished"}}));
            ended.insert(id);
            actions.push_back("closed interrupted run " + id + " as FAIL");
        }
    }
    for (const auto& line : actions) log("  recovery: " + line);
    return actions;
}

std::optional<Head> init_state(const std::string& reason, bool dry_run, const LogFn& log) {
    if (read_head())
        throw StateCorrupt("a state head already exists at " + head_path().string() + "; init-state runs once per host");
    struct Venue {
        std::string mic;
        numbering::Holdings holdings;
        std::vector<std::int64_t> token_order;
        std::string newest;
        std::string mode;
        Json header;
    };
    std::vector<config::Venue> cfgs;
    for (const auto& [k, c] : config::get().venues) cfgs.push_back(c);
    std::vector<Venue> holdings;
    for (const auto& cfg : cfgs) {
        std::string mic = cfg.mic;
        auto days = venue_days(mic);
        if (!cfg.venue_id || days.empty()) continue;
        std::string newest = days.back();
        auto entry = counter_token::venue_entry(newest, mic);
        Json header = counter_token::read_json(counter_token::venue_manifest_path(newest, mic));
        const Json& m = jget(jget(header, "numbering"), "mode");
        std::string mode = m.is_null() ? "live" : jstr(m);
        Venue v;
        v.mic = mic;
        v.newest = newest;
        v.mode = mode;
        v.header = header;
        v.holdings.venue_id = entry ? entry->venue_id : 0;
        if (entry) {
            for (const auto& [s, t] : entry->retained) v.holdings.assigned[s] = t;
            for (const auto& [s, t] : entry->assigned) v.holdings.assigned[s] = t;
            for (const auto& [s, t] : entry->retained) v.token_order.push_back(v.holdings.assigned[s]);
            for (const auto& [s, t] : entry->assigned)
                if (!entry->retained.count(s)) v.token_order.push_back(t);
            v.holdings.free = entry->free;
            std::sort(v.holdings.free.begin(), v.holdings.free.end());
            for (const auto& [s, t] : v.holdings.assigned) v.holdings.last_date[s] = newest;
        }
        holdings.push_back(std::move(v));
    }
    std::map<std::int64_t, std::string> owners;
    std::vector<std::string> clashes;
    for (const auto& v : holdings) {
        std::vector<std::int64_t> tokens = v.token_order;
        tokens.insert(tokens.end(), v.holdings.free.begin(), v.holdings.free.end());
        for (auto t : tokens) {
            auto [it, inserted] = owners.emplace(t, v.mic);
            if (it->second != v.mic) clashes.push_back(std::to_string(t) + " (" + it->second + " and " + v.mic + ")");
        }
    }
    if (!clashes.empty()) {
        std::vector<std::string> first(clashes.begin(), clashes.begin() + static_cast<std::ptrdiff_t>(std::min<std::size_t>(5, clashes.size())));
        throw StateCorrupt("venues share " + util::commas(static_cast<std::int64_t>(clashes.size())) + " token(s), e.g. " +
                           util::join(first, ", ") +
                           ". A day was numbered in isolation (the old code numbered an older date from 1). Fix those "
                           "days before initialising the state.");
    }
    auto [counter, source] = global_counter(nullptr);
    std::int64_t top = 0;
    for (const auto& v : holdings) top = std::max(top, v.holdings.highest());
    if (top > counter)
        throw StateCorrupt("a venue holds token " + util::commas(top) + " but the highest number on record is " +
                           util::commas(counter) + "; the counter would reissue it");
    for (const auto& v : holdings)
        log("  " + v.mic + ": newest " + v.newest + " (" + v.mode + "), " +
            util::commas(static_cast<std::int64_t>(v.holdings.assigned.size())) + " held, " +
            util::commas(static_cast<std::int64_t>(v.holdings.free.size())) + " free, highest " +
            util::commas(v.holdings.highest()));
    log("  counter: " + util::commas(counter) + " (from " + (source.empty() ? "nothing" : source) + ")");
    if (dry_run) {
        log("  DRY RUN: nothing written");
        return std::nullopt;
    }
    std::string newest_all = "00000000";
    if (!holdings.empty()) {
        newest_all = holdings.front().newest;
        for (const auto& v : holdings) newest_all = std::max(newest_all, v.newest);
    }
    std::string run_id = new_run_id(newest_all, "init");
    append_event(event("START", run_id, "init-state", {{"reason", reason}, {"operator", operator_id()}}));
    auto [archive, pruned] = backup(run_id);
    Head head;
    head.counter = counter;
    Json inputs = Json::array();
    for (const auto& v : holdings) {
        auto snap = write_snapshot(v.mic, run_id, v.holdings);
        VenueHead vh;
        vh.venue_id = v.holdings.venue_id;
        vh.snapshot = snap.relative;
        vh.sha256 = snap.sha256;
        vh.rows = snap.rows;
        vh.newest = v.newest;
        vh.newest_mode = v.mode;
        vh.live_date = v.mode == "live" ? v.newest : "";
        vh.run_id = run_id;
        vh.updated_at = counter_token::utc_now();
        head.venues[v.mic] = vh;
        const Json& allocation = jget(v.header, "allocation");
        std::string alloc_name = allocation.contains("path") ? jstr(allocation["path"]) : v.mic;
        inputs.push_back({{"path", v.newest + "/" + paths::TRANSFORM_DIR + "/manifests/" + alloc_name},
                          {"sha256", allocation.contains("sha256") ? allocation["sha256"] : Json("")},
                          {"rows", allocation.contains("rows") ? allocation["rows"] : Json(0)}});
    }
    head.last_run = {{"run_id", run_id},
                     {"command", "init-state"},
                     {"reason", reason},
                     {"operator", operator_id()},
                     {"completed_at", counter_token::utc_now()}};
    write_head(head);
    Json venues = Json::array();
    std::vector<std::string> mics;
    for (const auto& v : holdings) mics.push_back(v.mic);
    std::sort(mics.begin(), mics.end());
    for (const auto& m : mics) venues.push_back(m);
    Json pruned_json = Json::array();
    for (const auto& p : pruned) pruned_json.push_back(p);
    append_event(event("COMPLETE", run_id, "init-state",
                       {{"reason", reason},
                        {"operator", operator_id()},
                        {"counter", counter},
                        {"venues", venues},
                        {"backup", archive.filename().string()},
                        {"backups_pruned", pruned_json}},
                       inputs));
    log("  state initialised: " + head_path().string() + " (backup " + archive.filename().string() + ")");
    return head;
}

}  // namespace premarket::state
