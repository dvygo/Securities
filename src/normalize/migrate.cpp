#include "premarket/normalize/migrate.hpp"

#include <map>
#include <set>

#include "premarket/log.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::migrate {

using counter_token::jget;
using counter_token::jint;
using counter_token::jstr;
using jsonio::Json;

namespace {

struct Header {
    std::string date;
    std::string mic;
    std::filesystem::path path;
    Json doc;
    std::int64_t version = 0;
};

struct Sequence {
    std::filesystem::path path;
    Json doc;
    std::int64_t version = 0;
};

struct Snapshot {
    counter_token::TokenMap assigned, retained;
    std::vector<std::int64_t> free;
    bool operator==(const Snapshot& o) const { return assigned == o.assigned && retained == o.retained && free == o.free; }
};

Snapshot read_any(const std::string& date, const std::string& mic) {
    auto e = counter_token::venue_entry(date, mic, true);
    if (!e) throw state::StateCorrupt(date + " " + mic + ": header present but unreadable");
    std::sort(e->free.begin(), e->free.end());
    return {e->assigned, e->retained, e->free};
}

void write_json(const std::filesystem::path& path, const std::string& text) { state::atomic_write(path, text); }

}  // namespace

int migrate_state(const std::string& reason, bool dry_run) {
    state::Lock lock("migrate-state");
    auto head_path = state::head_path();
    std::error_code ec;
    if (!std::filesystem::exists(head_path, ec))
        throw state::StateMissing("no state head at " + head_path.string() +
                                  "; there is nothing to migrate. A host that never numbered runs `premarket init-state`.");
    Json head_doc = jsonio::read(head_path);
    std::int64_t head_version = jint(jget(head_doc, "version"), 0);
    if (head_version == state::HEAD_VERSION) {
        log::out("  " + head_path.string() + " is already v8; nothing to migrate");
        return 0;
    }
    if (head_version != FROM_HEAD_VERSION)
        throw state::StateCorrupt(head_path.string() + " is head version " + std::to_string(head_version) +
                                  "; migrate-state moves the 7.x state (version 1) to v8");

    std::vector<Header> headers;
    std::vector<Sequence> sequences;
    std::vector<std::string> refused;
    std::map<std::string, std::int64_t> by_version;
    for (const auto& day : counter_token::numbered_day_dirs()) {
        for (const auto& p : util::list_dir_sorted(counter_token::manifests_dir(day))) {
            std::string name = p.filename().string();
            if (!util::ends_with(name, ".json")) continue;
            Json doc = jsonio::read(p);
            std::int64_t v = jint(jget(doc, "version"), 0);
            if (name == "_sequence.json") {
                if (v != counter_token::MANIFEST_VERSION) sequences.push_back({p, doc, v});
                continue;
            }
            if (util::starts_with(name, "_")) continue;
            by_version["v" + std::to_string(v)] += 1;
            if (v == counter_token::MANIFEST_VERSION) continue;
            if (v != 4 && v != 5) {
                refused.push_back(day + "/" + name + " (version " + std::to_string(v) + ")");
                continue;
            }
            headers.push_back({day, name.substr(0, name.size() - 5), p, doc, v});
        }
    }
    if (!refused.empty())
        throw state::StateCorrupt("manifests older than version 4 cannot be migrated: " + util::join(refused, ", ") +
                                  ". Run 7.x's `migrate-manifests` on them first, or remove those days from the tree.");

    auto head = state::Head::from_dict(head_doc);
    std::map<std::string, numbering::Holdings> holdings_before;
    for (const auto& [mic, vh] : head.venues) holdings_before[mic] = state::load_snapshot(mic, vh);
    std::map<std::pair<std::string, std::string>, Snapshot> allocations_before;
    for (const auto& h : headers) allocations_before[{h.date, h.mic}] = read_any(h.date, h.mic);

    std::vector<std::string> versions;
    for (const auto& [v, n] : by_version) versions.push_back(v + "=" + util::commas(n));
    log::out("  state head " + head_path.string() + ": version 1, counter " + util::commas(head.counter) + ", " +
             std::to_string(head.venues.size()) + " venue(s), snapshots verified");
    log::out("  manifests: " + std::to_string(headers.size()) + " venue header(s) to move to v8 (" +
             (versions.empty() ? std::string("none") : util::join(versions, ", ")) + "), " + std::to_string(sequences.size()) +
             " _sequence.json file(s); every allocation table verified against its header");
    if (dry_run) {
        log::out("  DRY RUN: nothing written");
        return 0;
    }

    std::string run_id = state::new_run_id(util::utc_format_ns(util::unix_now_ns(), "%Y%m%d"), "migrate");
    state::append_event(state::event("START", run_id, "migrate-state", {{"reason", reason}, {"operator", state::operator_id()}}));
    auto [archive, pruned] = state::backup(run_id);
    log::out("  backup: " + archive.string());
    std::string at = counter_token::utc_now();
    Json migrated = {{"from_version", 0}, {"by", "premarket/" + counter_token::build_sha()}, {"at", at}, {"run_id", run_id}};
    for (auto& h : headers) {
        Json doc = h.doc;
        Json m = migrated;
        m["from_version"] = h.version;
        doc["version"] = counter_token::MANIFEST_VERSION;
        if (doc.contains("code") && doc["code"].is_object()) doc["code"]["manifest_version"] = counter_token::MANIFEST_VERSION;
        doc["migrated"] = m;
        write_json(h.path, jsonio::pretty(doc));
    }
    for (auto& s : sequences) {
        Json doc = s.doc;
        doc["version"] = counter_token::MANIFEST_VERSION;
        write_json(s.path, jsonio::line(doc));
    }
    head.version = state::HEAD_VERSION;
    head.last_run = {{"run_id", run_id}, {"command", "migrate-state"}, {"reason", reason},
                     {"operator", state::operator_id()}, {"completed_at", counter_token::utc_now()}, {"from_version", 1}};
    state::write_head(head);

    auto check = state::require_head();
    std::vector<std::string> problems;
    for (const auto& [mic, vh] : check.venues) {
        auto now = state::load_snapshot(mic, vh);
        const auto& was = holdings_before.at(mic);
        if (now.assigned != was.assigned || now.free != was.free || now.last_date != was.last_date)
            problems.push_back(mic + " state snapshot differs after migration");
    }
    for (const auto& [key, was] : allocations_before) {
        auto e = counter_token::venue_entry(key.first, key.second);
        Snapshot now{e->assigned, e->retained, e->free};
        std::sort(now.free.begin(), now.free.end());
        if (!(now == was)) problems.push_back(key.first + " " + key.second + " allocation differs after migration");
    }
    Json p = Json::array();
    for (const auto& x : problems) p.push_back(x);
    Json pr = Json::array();
    for (const auto& x : pruned) pr.push_back(x);
    state::append_event(state::event(problems.empty() ? "COMPLETE" : "FAIL", run_id, "migrate-state",
                                     {{"reason", reason}, {"operator", state::operator_id()},
                                      {"headers", static_cast<std::int64_t>(headers.size())},
                                      {"sequences", static_cast<std::int64_t>(sequences.size())},
                                      {"backup", archive.filename().string()}, {"backups_pruned", pr}, {"problems", p}}));
    if (!problems.empty()) {
        for (const auto& x : problems) log::err("error: " + x);
        log::err("error: migration verification failed; restore with `tar -xzf " + archive.string() + "` from " +
                 paths::data_root().string());
        return 1;
    }
    log::out("  migrated: head v8, " + std::to_string(headers.size()) + " header(s), " + std::to_string(sequences.size()) +
             " sequence file(s); " + std::to_string(check.venues.size()) + " venue state(s) and " +
             std::to_string(allocations_before.size()) + " day allocation(s) verified unchanged");
    return 0;
}

}  // namespace premarket::migrate
