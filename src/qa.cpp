#include "premarket/qa.hpp"

#include <databento/dbn_store.hpp>
#include <databento/enums.hpp>

#include <algorithm>
#include <cstring>
#include <map>
#include <set>
#include <unordered_map>

#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/plugin.hpp"
#include "premarket/util.hpp"

namespace premarket::qa {

using counter_token::jget;
using counter_token::jint;
using counter_token::jstr;
using counter_token::truthy;
using jsonio::Json;

namespace {

std::string pad(const std::string& s, std::size_t width) {
    return s.size() >= width ? s : s + std::string(width - s.size(), ' ');
}

std::string c(std::int64_t n) { return util::commas(n); }
std::string cz(std::size_t n) { return util::commas(static_cast<std::int64_t>(n)); }

std::map<std::string, std::vector<std::filesystem::path>> venue_files(const std::string& date_dir) {
    std::map<std::string, std::vector<std::filesystem::path>> found;
    auto dir = paths::normalized_dir(date_dir);
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return found;
    for (const auto& p : util::list_dir_sorted(dir)) {
        std::string name = p.filename().string();
        if (!util::ends_with(name, ".parquet")) continue;
        found[util::upper(name.substr(0, name.find('-')))].push_back(p);
    }
    return found;
}

struct Columns {
    std::vector<std::vector<parquet::Cell>> cols;
    std::size_t rows = 0;
};

Columns read_columns(const std::vector<std::filesystem::path>& files, const std::vector<std::string>& names) {
    Columns out;
    out.cols.resize(names.size());
    for (const auto& f : files) {
        auto t = parquet::read_table(f, names);
        for (const auto& row : t.rows)
            for (std::size_t i = 0; i < names.size(); ++i) out.cols[i].push_back(row[i]);
        out.rows += t.rows.size();
    }
    return out;
}

using Pairs = std::set<std::pair<std::string, std::string>>;

Pairs distinct_pairs(const std::vector<parquet::Cell>& a, const std::vector<parquet::Cell>& b) {
    Pairs out;
    for (std::size_t i = 0; i < a.size(); ++i) out.insert({a[i].value_or("\x01null"), b[i].value_or("\x01null")});
    return out;
}

std::pair<std::optional<counter_token::VenueEntry>, std::string> entry_of(const std::string& date_dir, const std::string& mic) {
    try {
        return {counter_token::venue_entry(date_dir, mic), ""};
    } catch (const counter_token::ManifestCorrupt& exc) {
        return {std::nullopt, exc.what()};
    }
}

struct Alloc {
    counter_token::TokenMap assigned;
    std::vector<std::int64_t> free;
};

std::optional<Alloc> allocation(const std::string& date_dir, const std::string& mic) {
    auto [entry, why] = entry_of(date_dir, mic);
    if (!entry) return std::nullopt;
    Alloc a;
    a.assigned = entry->retained;
    for (const auto& [s, t] : entry->assigned) a.assigned[s] = t;
    a.free = entry->free;
    std::sort(a.free.begin(), a.free.end());
    return a;
}

Json numbering_block(const std::string& date_dir, const std::string& mic) {
    Json header = counter_token::read_json(counter_token::venue_manifest_path(date_dir, mic));
    const Json& n = jget(header, "numbering");
    return truthy(n) ? n : Json::object();
}

std::optional<numbering::Holdings> snapshot_of(const std::string& mic, const Json& recorded) {
    if (!truthy(recorded) || !truthy(jget(recorded, "snapshot"))) return std::nullopt;
    state::VenueHead vh;
    vh.venue_id = 0;
    vh.snapshot = jstr(recorded["snapshot"]);
    vh.sha256 = jstr(jget(recorded, "sha256"), "");
    return state::load_snapshot(mic, vh);
}

std::vector<Check> recycling(const std::string& span, const std::string& mic, const Alloc& before, const Alloc& after,
                             const std::string& against) {
    std::set<std::string> kept, departed, arrived;
    for (const auto& [s, t] : before.assigned) (after.assigned.count(s) ? kept : departed).insert(s);
    for (const auto& [s, t] : after.assigned)
        if (!kept.count(s)) arrived.insert(s);
    std::set<std::int64_t> released;
    for (const auto& s : departed) released.insert(before.assigned.at(s));
    std::set<std::int64_t> available(before.free.begin(), before.free.end());
    available.insert(released.begin(), released.end());
    std::vector<std::int64_t> taken;
    std::size_t extended = 0;
    for (const auto& s : arrived) {
        auto t = after.assigned.at(s);
        if (available.count(t)) taken.push_back(t);
        else ++extended;
    }
    std::set<std::int64_t> taken_set(taken.begin(), taken.end());
    std::vector<std::int64_t> carried;
    for (auto t : available)
        if (!taken_set.count(t)) carried.push_back(t);
    std::set<std::int64_t> after_tokens;
    for (const auto& [s, t] : after.assigned) after_tokens.insert(t);
    bool disjoint = std::none_of(released.begin(), released.end(), [&](std::int64_t t) { return after_tokens.count(t); });
    return {
        {span, mic, "offsets released", disjoint || !taken.empty(),
         cz(departed.size()) + " script(s) departed, " + cz(released.size()) + " offset(s) released; pool in " +
             cz(before.free.size()) + " -> " + cz(available.size()) + " available (" + against + ")",
         true, V2},
        {span, mic, "pool drained first", taken.size() == std::min(arrived.size(), available.size()),
         cz(arrived.size()) + " arrival(s): " + cz(taken.size()) + " reused a released token (available " +
             cz(available.size()) + "), " + cz(extended) + " drew a new one from the shared sequence",
         true, V2},
        {span, mic, "pool carried", after.free == carried,
         cz(after.free.size()) + " offset(s) still free for tomorrow (expected " + cz(carried.size()) + ")", true, V2},
    };
}

std::vector<Check> check_fill(const std::string& date_dir, const std::string& mic, const Json& block) {
    std::optional<numbering::Holdings> before, after;
    try {
        before = snapshot_of(mic, jget(block, "state_before"));
        after = snapshot_of(mic, jget(block, "state_after"));
    } catch (const std::exception& exc) {
        return {{date_dir, mic, "fill snapshots", false, exc.what(), true, V2}};
    }
    std::vector<Check> checks;
    if (before && after) {
        std::vector<std::string> moved;
        for (const auto& [s, t] : before->assigned) {
            auto it = after->assigned.find(s);
            if (it == after->assigned.end() || it->second != t) moved.push_back(s);
        }
        checks.push_back({date_dir, mic, "never releases", moved.empty(),
                          cz(before->assigned.size()) + " holding(s) before, " + cz(moved.size()) + " changed or released" +
                              (moved.empty() ? "" : " (e.g. " + moved[0] + ")"),
                          true, V2});
    }
    auto [entry, why] = entry_of(date_dir, mic);
    if (!why.empty() || !entry) {
        checks.push_back({date_dir, mic, "fill provenance", false, why.empty() ? "no allocation for a filled day" : why, true, V2});
        return checks;
    }
    std::set<std::int64_t> legit;
    const Json& neighbours = jget(block, "neighbours");
    for (const char* key : {"earlier", "later"}) {
        std::string day = jstr(jget(neighbours, key), "");
        if (day.empty()) continue;
        auto [other, other_why] = entry_of(day, mic);
        if (!other_why.empty() || !other) {
            checks.push_back({date_dir, mic, "filled from", false,
                              std::string(key) + " neighbour " + day + " has no readable manifest -- the fill's basis is gone",
                              true, V2});
            continue;
        }
        for (const auto& [s, t] : other->assigned) legit.insert(t);
    }
    if (before) {
        for (const auto& [s, t] : before->assigned) legit.insert(t);
        legit.insert(before->free.begin(), before->free.end());
    }
    std::int64_t floor = jint(jget(block, "counter_before"), 0);
    counter_token::TokenMap held = entry->retained;
    for (const auto& [s, t] : entry->assigned) held[s] = t;
    std::vector<std::string> stray;
    for (const auto& [s, t] : held)
        if (!legit.count(t) && t <= floor) stray.push_back(s);
    checks.push_back({date_dir, mic, "fill provenance", stray.empty(),
                      stray.empty() ? cz(held.size()) + " token(s): every one from a neighbour, the state, its free pool, or "
                                                        "above the counter (" + c(floor) + ")"
                                    : cz(stray.size()) + " token(s) with no legitimate source, e.g. " + stray[0],
                      true, V2});
    return checks;
}

std::vector<Check> check_manifest(const std::string& date_dir, const std::string& mic, const config::Venue& cfg,
                                  const Pairs& pairs, const std::optional<counter_token::VenueEntry>& entry) {
    if (!entry)
        return {{date_dir, mic, "manifest present", false,
                 "a normalized parquet exists but the manifest does not name this venue -- tomorrow will renumber from scratch"}};
    const auto& assigned = entry->assigned;
    const auto& free = entry->free;
    const auto& retained = entry->retained;
    std::vector<std::string> problems;
    if (entry->venue_id != cfg.venue_id)
        problems.push_back("venue_id " + std::to_string(entry->venue_id) + " != config " + std::to_string(cfg.venue_id));
    if (entry->count != static_cast<std::int64_t>(assigned.size()))
        problems.push_back("count " + std::to_string(entry->count) + " != " + std::to_string(assigned.size()) + " assigned");
    std::int64_t over = 0, highest = 0;
    std::set<std::int64_t> assigned_tokens;
    for (const auto& [s, t] : assigned) {
        if (t > counter_token::INT32_MAX_TOKEN) ++over;
        highest = std::max(highest, t);
        assigned_tokens.insert(t);
    }
    if (over) problems.push_back(c(over) + " token(s) past int32");
    std::set<std::int64_t> free_set(free.begin(), free.end());
    std::int64_t reissued = 0;
    for (auto t : free_set)
        if (assigned_tokens.count(t)) ++reissued;
    if (reissued) problems.push_back(c(reissued) + " offset(s) in both free and assigned -- the pool would hand out a live number");
    std::set<std::int64_t> retained_tokens;
    for (const auto& [s, t] : retained) retained_tokens.insert(t);
    std::int64_t doubled = 0;
    for (auto t : retained_tokens)
        if (assigned_tokens.count(t) || free_set.count(t)) ++doubled;
    if (doubled)
        problems.push_back(c(doubled) + " retained token(s) also assigned or free -- a number already handed out on this date "
                                        "would be handed out again");
    std::vector<Check> checks;
    checks.push_back({date_dir, mic, "manifest internal", problems.empty(),
                      problems.empty() ? "highest " + c(highest) + ", " + cz(assigned.size()) + " assigned, " +
                                             (retained.empty() ? "" : cz(retained.size()) + " retained, ") + cz(free.size()) + " free"
                                       : util::join(problems, "; ")});
    std::int64_t missing = 0, wrong = 0;
    for (const auto& [script, token] : pairs) {
        auto it = assigned.find(script);
        if (it == assigned.end()) {
            ++missing;
            continue;
        }
        std::string rendered;
        try {
            rendered = counter_token::assign(it->second);
        } catch (const ValueError&) {
            rendered = "\x01none";
        }
        if (rendered != token) ++wrong;
    }
    std::int64_t orphans = static_cast<std::int64_t>(assigned.size()) - (static_cast<std::int64_t>(pairs.size()) - missing);
    checks.push_back({date_dir, mic, "manifest agrees", missing == 0 && wrong == 0 && orphans == 0,
                      c(missing) + " script(s) in the file but not the manifest, " + c(wrong) +
                          " token(s) that do not re-derive, " + c(orphans) + " allocated to script(s) the file does not carry" +
                          (retained.empty() ? "" : "; " + cz(retained.size()) + " retained from an earlier run of the date, correctly absent")});
    return checks;
}

bool numeric(const std::string& s) { return util::is_digits(s); }

std::vector<Check> token_day(const std::string& date_dir, const std::set<std::string>& wanted) {
    auto files = venue_files(date_dir);
    std::map<std::string, counter_token::VenueEntry> entries;
    std::map<std::string, std::string> unreadable;
    for (const auto& mic : counter_token::venues_with_manifest(date_dir)) {
        auto [entry, why] = entry_of(date_dir, mic);
        if (!why.empty()) unreadable[mic] = why;
        else if (entry) entries[mic] = *entry;
    }
    std::vector<Check> checks;
    for (const auto& [mic, why] : unreadable) checks.push_back({date_dir, mic, "manifest readable", false, why});
    std::map<std::string, std::set<std::string>> seen_tokens;
    for (const auto& [mic, e] : entries) {
        if (files.count(mic) || !wanted.count(mic)) continue;
        checks.push_back({date_dir, mic, "manifest has data", false,
                          "manifest allocates " + cz(e.assigned.size()) +
                              " script(s) but no normalized parquet exists -- the next day will still carry this allocation forward"});
    }
    for (const auto& [mic, paths_] : files) {
        if (!wanted.count(mic)) continue;
        const auto* cfg = config::venue(mic);
        if (cfg == nullptr || !cfg->venue_id) continue;
        auto cols = read_columns(paths_, {"script", "counterToken", "counterTokenV2"});
        if (!cols.rows) continue;
        const auto& v2 = cols.cols[2];
        std::size_t blank = 0, bad = 0;
        std::int64_t widest = 0;
        for (const auto& t : v2) {
            if (!t || t->empty()) ++blank;
            if (!t || !numeric(*t)) {
                ++bad;
            } else if (t->size() <= 18) {
                widest = std::max<std::int64_t>(widest, std::stoll(*t));
            } else {
                widest = std::max<std::int64_t>(widest, INT64_MAX);
            }
        }
        checks.push_back({date_dir, mic, "populated", blank == 0,
                          cz(cols.rows) + " row(s), " + cz(blank) + " blank" +
                              (blank ? " -- a blank token means the script was not in the day's allocation, so the manifest and the file disagree" : "")});
        checks.push_back({date_dir, mic, "numeric int32", bad == 0 && widest <= counter_token::INT32_MAX_TOKEN,
                          cz(bad) + " non-numeric, highest " + c(widest) + " (int32 " + c(counter_token::INT32_MAX_TOKEN) + ")"});
        auto pairs = distinct_pairs(cols.cols[0], v2);
        std::set<std::string> scripts, tokens;
        for (const auto& [s, t] : pairs) {
            if (s != "\x01null") scripts.insert(s);
            if (t != "\x01null") tokens.insert(t);
        }
        checks.push_back({date_dir, mic, "one-to-one", pairs.size() == scripts.size() && scripts.size() == tokens.size(),
                          cz(pairs.size()) + " distinct pair(s), " + cz(scripts.size()) + " script(s), " + cz(tokens.size()) + " token(s)"});
        for (const auto& [s, t] : pairs) seen_tokens[mic].insert(t);
        std::optional<counter_token::VenueEntry> e;
        if (auto it = entries.find(mic); it != entries.end()) e = it->second;
        for (auto& ch : check_manifest(date_dir, mic, *cfg, pairs, e)) checks.push_back(ch);
        Json block = numbering_block(date_dir, mic);
        if (jstr(jget(block, "mode"), "") == "fill")
            for (auto& ch : check_fill(date_dir, mic, block)) checks.push_back(ch);
    }
    for (const auto& [mic, mine] : seen_tokens) {
        std::vector<std::string> clashes, others;
        for (const auto& [other, files_] : files) {
            if (other == mic) continue;
            const auto* ocfg = config::venue(other);
            if (ocfg == nullptr || !ocfg->venue_id) continue;
            others.push_back(other);
            auto theirs = read_columns(files_, {"counterTokenV2"});
            std::size_t shared = 0;
            std::set<std::string> counted;
            for (const auto& t : theirs.cols[0])
                if (t && !t->empty() && mine.count(*t) && counted.insert(*t).second) ++shared;
            if (shared) clashes.push_back(cz(shared) + " with " + other);
        }
        checks.push_back({date_dir, mic, "venues disjoint", clashes.empty(),
                          clashes.empty() ? "no token shared with " + (others.empty() ? std::string("any other venue") : util::join(others, ", "))
                                          : "token(s) shared: " + util::join(clashes, "; ")});
    }
    if (auto issued = counter_token::load_sequence(date_dir)) {
        std::int64_t highest = 0;
        for (const auto& [mic, tokens] : seen_tokens)
            for (const auto& t : tokens)
                if (numeric(t) && t.size() <= 18) highest = std::max<std::int64_t>(highest, std::stoll(t));
        checks.push_back({date_dir, "*", "sequence covers", highest <= *issued,
                          "sequence issued up to " + c(*issued) + "; highest token in any file " + c(highest) +
                              (highest <= *issued ? "" : " -- a file carries a token the sequence never issued, so a later day could hand that number to another instrument")});
    }
    for (auto& ch : checks) ch.tag = V2;
    return checks;
}

std::pair<std::set<std::string>, std::vector<std::string>> recorded_moves(const std::string& previous, const std::string& current,
                                                                          const std::string& mic) {
    std::set<std::string> allowed;
    std::vector<std::string> problems;
    for (auto [day, other] : {std::pair{current, previous}, std::pair{previous, current}}) {
        try {
            for (const auto& r : counter_token::read_exceptions(day, mic))
                if (r.wanted_from == other) allowed.insert(r.script);
        } catch (const counter_token::ManifestCorrupt& exc) {
            problems.push_back(exc.what());
        }
    }
    return {allowed, problems};
}

std::vector<Check> token_pair(const std::string& previous, const std::string& current, const std::string& mic) {
    std::vector<Check> checks;
    auto before = venue_files(previous), after = venue_files(current);
    if (!before.count(mic) || !after.count(mic)) return checks;
    std::string span = previous + " -> " + current;
    auto a_cols = read_columns(before[mic], {"script", "counterTokenV2"});
    auto b_cols = read_columns(after[mic], {"script", "counterTokenV2"});
    auto old_pairs = distinct_pairs(a_cols.cols[0], a_cols.cols[1]);
    auto new_pairs = distinct_pairs(b_cols.cols[0], b_cols.cols[1]);
    std::multimap<std::string, std::string> new_by_script, new_by_token;
    for (const auto& [s, t] : new_pairs) {
        new_by_script.insert({s, t});
        new_by_token.insert({t, s});
    }
    std::size_t shared = 0, ambiguous = 0;
    std::set<std::string> moved, joined_scripts;
    for (const auto& [s, t] : old_pairs) {
        auto range = new_by_script.equal_range(s);
        for (auto it = range.first; it != range.second; ++it) {
            ++shared;
            joined_scripts.insert(s);
            if (it->second != t) moved.insert(s);
        }
        auto trange = new_by_token.equal_range(t);
        for (auto it = trange.first; it != trange.second; ++it)
            if (it->second != s) ++ambiguous;
    }
    auto [allowed, unreadable] = recorded_moves(previous, current, mic);
    for (const auto& why : unreadable) checks.push_back({span, mic, "exceptions readable", false, why});
    std::set<std::string> unrecorded, unmoved;
    for (const auto& s : moved)
        if (!allowed.count(s)) unrecorded.insert(s);
    if (!allowed.empty())
        for (const auto& s : allowed)
            if (joined_scripts.count(s) && !moved.count(s)) unmoved.insert(s);
    std::size_t moved_allowed = 0;
    for (const auto& s : moved)
        if (allowed.count(s)) ++moved_allowed;
    std::string detail = cz(shared) + " script(s) on both days, " + cz(moved.size()) + " moved to a different token";
    if (!moved.empty() && unrecorded.empty()) detail += " -- all " + cz(moved_allowed) + " recorded as exceptions for this pair";
    if (!unrecorded.empty())
        detail += " -- " + cz(unrecorded.size()) + " with no recorded exception, the carry-forward chain is broken (e.g. " +
                  *unrecorded.begin() + ")";
    if (!unmoved.empty()) detail += " -- " + cz(unmoved.size()) + " recorded as exceptions but did not move";
    checks.push_back({span, mic, "stable", unrecorded.empty() && unmoved.empty(), detail});
    checks.push_back({span, mic, "no reuse", ambiguous == 0,
                      cz(ambiguous) + " token(s) name a different script on the two days" +
                          (ambiguous ? " -- v2 recycles a departed script's offset, so this is expected, not a regression; a "
                                       "cross-date join on the token must carry the date with it"
                                     : ""),
                      false});
    Json was = numbering_block(previous, mic), now = numbering_block(current, mic);
    if (jstr(jget(now, "mode"), "") == "live" && truthy(jget(jget(now, "state_before"), "snapshot"))) {
        try {
            auto snap = snapshot_of(mic, now["state_before"]);
            Alloc start{snap->assigned, snap->free};
            std::sort(start.free.begin(), start.free.end());
            auto cur = allocation(current, mic);
            if (!cur) throw RuntimeError("no allocation for " + current);
            for (auto& ch : recycling(span, mic, start, *cur, "from the state it started from")) checks.push_back(ch);
        } catch (const std::exception& exc) {
            checks.push_back({span, mic, "recycling", false, exc.what()});
        }
    } else if (!truthy(was) && !truthy(now)) {
        auto b = allocation(previous, mic), a = allocation(current, mic);
        if (!b || !a)
            checks.push_back({span, mic, "recycling", false, "one of the two days has no manifest entry for this venue", false, V2});
        else
            for (auto& ch : recycling(span, mic, *b, *a, "from " + previous)) checks.push_back(ch);
    }
    if (truthy(was) || truthy(now)) {
        std::vector<std::pair<std::string, Json>> filled;
        for (const auto& [day, block] : {std::pair{previous, was}, std::pair{current, now}})
            if (jstr(jget(block, "mode"), "") == "fill") filled.push_back({day, block});
        if (!filled.empty()) {
            bool ok = false;
            std::vector<std::string> parts;
            for (const auto& [day, block] : filled) {
                std::set<std::string> names;
                const Json& n = jget(block, "neighbours");
                if (n.is_object())
                    for (auto it = n.begin(); it != n.end(); ++it)
                        if (it.value().is_string() && !it.value().get<std::string>().empty()) names.insert(it.value().get<std::string>());
                std::set<std::string> need = {previous, current};
                need.erase(day);
                if (std::includes(names.begin(), names.end(), need.begin(), need.end())) ok = true;
                parts.push_back(day + " filled from " + (names.empty() ? "nothing" : util::join({names.begin(), names.end()}, ", ")));
            }
            checks.push_back({span, mic, "filled from", ok,
                              util::join(parts, "; ") +
                                  (ok ? "" : " -- a day was numbered between them after the fill; its records for this pair are superseded"),
                              false});
        } else {
            std::string rid = jstr(jget(now, "run_id"), "");
            checks.push_back({span, mic, "chained from", jstr(jget(now, "mode"), "") == "live",
                              current + " continued the venue's state" +
                                  (truthy(now) ? " (numbered by run " + rid.substr(0, std::min<std::size_t>(32, rid.size())) + ")" : "")});
        }
    } else if (const auto* cfg = config::venue(mic); cfg && cfg->venue_id) {
        try {
            auto [source, stamp] = counter_token::previous_tokens(current, mic, cfg->venue_id);
            checks.push_back({span, mic, "chained from", source.has_value() && stamp == previous,
                              "carried from " + (stamp.empty() ? std::string("nothing -- renumbered from scratch") : stamp) +
                                  (stamp == previous ? "" : ", not from " + previous + "; the days between were numbered against an older allocation")});
        } catch (const ValueError& exc) {
            checks.push_back({span, mic, "chained from", false, exc.what()});
        }
    }
    for (auto& ch : checks) ch.tag = V2;
    return checks;
}

struct RawScan {
    struct File {
        std::string name, dataset, schema;
        std::int64_t start = 0, end = 0, records = 0;
    };
    std::vector<File> files;
    std::int64_t records = 0, duplicates = 0, blank_symbol = 0;
    std::unordered_map<std::uint32_t, std::string> symbols;
};

RawScan scan_raw(const std::vector<std::filesystem::path>& files, const std::string& dataset) {
    namespace db = databento;
    RawScan scan;
    for (const auto& path : files) {
        db::DbnStore store{path};
        const auto& meta = store.GetMetadata();
        RawScan::File f;
        f.name = path.filename().string();
        f.dataset = meta.dataset;
        f.schema = meta.schema ? std::string(db::ToString(*meta.schema)) : "None";
        f.start = static_cast<std::int64_t>(meta.start.time_since_epoch().count());
        f.end = static_cast<std::int64_t>(meta.end.time_since_epoch().count());
        bool skipped = f.schema != "definition" || f.dataset != dataset;
        if (!skipped) {
            while (const db::Record* rec = store.NextRecord()) {
                if (!rec->Holds<db::InstrumentDefMsg>()) continue;
                const auto& d = rec->Get<db::InstrumentDefMsg>();
                ++scan.records;
                ++f.records;
                if (scan.symbols.count(d.hd.instrument_id)) {
                    ++scan.duplicates;
                    continue;
                }
                std::string symbol(d.raw_symbol.data(), strnlen(d.raw_symbol.data(), d.raw_symbol.size()));
                if (symbol.empty()) ++scan.blank_symbol;
                scan.symbols[d.hd.instrument_id] = symbol;
            }
        }
        scan.files.push_back(f);
    }
    return scan;
}

std::vector<Check> lineage_day(const std::string& date_dir, const std::set<std::string>& wanted) {
    std::vector<Check> checks;
    auto ndir = paths::normalized_dir(date_dir), pdir = paths::plugin_dir(date_dir);
    std::int64_t day_start = util::day_start_ns(date_dir), day_end = day_start + 86400000000000LL;
    std::error_code ec;
    for (const auto& [mic, cfg] : config::get().venues) {
        if (!wanted.count(mic)) continue;
        std::vector<std::filesystem::path> matches;
        if (std::filesystem::is_directory(ndir, ec))
            for (const auto& p : util::list_dir_sorted(ndir))
                if (util::starts_with(p.filename().string(), mic + "-") && util::ends_with(p.filename().string(), ".parquet"))
                    matches.push_back(p);
        if (matches.empty()) continue;
        auto normalized = matches.front();
        std::vector<std::filesystem::path> raw;
        auto vdir = paths::venue_dir(date_dir, mic);
        if (std::filesystem::is_directory(vdir, ec))
            for (const auto& p : util::list_dir_sorted(vdir)) {
                std::string n = p.filename().string();
                if (util::ends_with(n, ".dbn") || util::ends_with(n, ".dbn.zst")) raw.push_back(p);
            }
        if (!raw.empty()) {
            auto scan = scan_raw(raw, cfg.dataset);
            std::vector<std::string> misplaced, wrong, sessions, clamped;
            for (const auto& f : scan.files) {
                if (!(day_start <= f.start && f.start < day_end))
                    misplaced.push_back(f.name + " starts " + util::utc_format_ns(f.start, "%Y-%m-%d"));
                if (f.dataset != cfg.dataset || f.schema != "definition")
                    wrong.push_back(f.name + " is " + f.dataset + "/" + f.schema + ", expected " + cfg.dataset +
                                    "/definition (skipped, so its rows are missing)");
                sessions.push_back(f.name + " (" + c(f.records) + ")");
                if (f.end && f.end < day_end) clamped.push_back(f.name + " ends " + util::utc_format_ns(f.end, "%H:%MZ"));
            }
            checks.push_back({date_dir, mic, "raw is this day", misplaced.empty(),
                              std::to_string(raw.size()) + " file(s); " +
                                  (misplaced.empty() ? "every window starts inside the day"
                                                     : util::join(misplaced, "; ") + " -- normalized as this day's data")});
            checks.push_back({date_dir, mic, "raw is this venue", wrong.empty(),
                              wrong.empty() ? cfg.dataset + "/definition" : util::join(wrong, "; ")});
            checks.push_back({date_dir, mic, "one session", scan.files.size() <= 1,
                              util::join(sessions, ", ") + (scan.files.size() > 1 ? " -- merged into one day's output" : ""), false});
            checks.push_back({date_dir, mic, "full day", clamped.empty(),
                              clamped.empty() ? "00:00Z..24:00Z" : util::join(clamped, "; ") + " (download clamped to what had published)",
                              false});
            auto names = parquet::column_names(normalized);
            std::vector<std::string> want = {"script", "scriptToken", "counterTokenV2"}, have, missing;
            for (const auto& w : want) (std::find(names.begin(), names.end(), w) != names.end() ? have : missing).push_back(w);
            auto t = parquet::read_table(normalized, have);
            int is = t.index("script"), it = t.index("scriptToken");
            std::int64_t kept = static_cast<std::int64_t>(scan.symbols.size()) - scan.blank_symbol;
            checks.push_back({date_dir, mic, "row accounting", static_cast<std::int64_t>(t.rows.size()) == kept,
                              c(scan.records) + " raw record(s) - " + c(scan.duplicates) + " duplicate id(s) - " +
                                  c(scan.blank_symbol) + " with no symbol = " + c(kept) + ", file has " + cz(t.rows.size())});
            std::set<std::string> tokens;
            std::vector<std::string> orphans;
            std::int64_t mismatched = 0;
            for (const auto& row : t.rows) {
                std::string token = it >= 0 ? row[static_cast<std::size_t>(it)].value_or("") : "";
                std::string script = is >= 0 ? row[static_cast<std::size_t>(is)].value_or("") : "";
                if (tokens.insert(token).second) {
                    auto id = util::parse_int(token);
                    if (!id || *id < 0 || *id > 0xFFFFFFFFLL || !scan.symbols.count(static_cast<std::uint32_t>(*id)))
                        orphans.push_back(token);
                }
                if (auto id = util::parse_int(token); id && *id >= 0 && *id <= 0xFFFFFFFFLL) {
                    auto s = scan.symbols.find(static_cast<std::uint32_t>(*id));
                    if (s != scan.symbols.end() && s->second != script) ++mismatched;
                }
            }
            std::vector<std::string> sample(orphans.begin(), orphans.begin() + static_cast<std::ptrdiff_t>(std::min<std::size_t>(3, orphans.size())));
            checks.push_back({date_dir, mic, "no invented rows", orphans.empty(),
                              cz(tokens.size()) + " distinct scriptToken(s), " + cz(orphans.size()) +
                                  " with no matching instrument_id in the raw file" +
                                  (orphans.empty() ? "" : " (e.g. " + util::join(sample, ", ") + ")")});
            checks.push_back({date_dir, mic, "symbol carried", mismatched == 0,
                              c(mismatched) + " row(s) whose script differs from the raw_symbol on the same instrument_id"});
            if (!missing.empty())
                checks.push_back({date_dir, mic, "schema current", false,
                                  "written without " + util::join(missing, ", ") +
                                      " -- a positional reader of the normalized columns would read this file shifted",
                                  false});
        } else {
            checks.push_back({date_dir, mic, "raw present", true,
                              "no DBN drop -- normalized from a streamed source, lineage starts here", false});
        }
        auto built = pdir / normalized.filename();
        if (std::filesystem::exists(built, ec)) {
            auto src = parquet::read_table(normalized, {"script", "counterTokenV2", "def_expiration", "expiration"});
            std::int64_t cutoff = day_start, expired = 0;
            std::map<std::string, std::string> carried;
            for (const auto& row : src.rows) {
                plugin::Normalized n = {{"def_expiration", row[2].value_or("")}, {"expiration", row[3].value_or("")}};
                if (plugin::is_expired(n, cutoff)) ++expired;
                carried[row[0].value_or("")] = row[1].value_or("");
            }
            auto out = parquet::read_table(built);
            checks.push_back({date_dir, mic, "strip accounting",
                              static_cast<std::int64_t>(src.rows.size()) - expired == static_cast<std::int64_t>(out.rows.size()),
                              cz(src.rows.size()) + " normalized - " + c(expired) + " expired before " + date_dir + " 00:00Z = " +
                                  c(static_cast<std::int64_t>(src.rows.size()) - expired) + ", plugin has " + cz(out.rows.size())});
            int iname = out.index("name"), itok = out.index("token");
            std::int64_t broken = 0;
            for (const auto& row : out.rows) {
                auto it2 = carried.find(row[static_cast<std::size_t>(iname)].value_or(""));
                if (it2 == carried.end() || it2->second != row[static_cast<std::size_t>(itok)].value_or("")) ++broken;
            }
            checks.push_back({date_dir, mic, "token carried", broken == 0,
                              c(broken) + " plugin row(s) whose token is not the counterTokenV2 of the normalized row with the same script",
                              true, V2});
            std::vector<std::string> empties;
            for (std::size_t col = 0; col < out.columns.size(); ++col) {
                std::int64_t n = 0;
                for (const auto& row : out.rows)
                    if (!row[col] || row[col]->empty()) ++n;
                if (n) empties.push_back(out.columns[col] + "=" + c(n));
            }
            checks.push_back({date_dir, mic, "no empty column", empties.empty(),
                              empties.empty() ? std::to_string(out.columns.size()) + " column(s), none empty on " + cz(out.rows.size()) + " rows"
                                              : util::join(empties, ", ")});
        }
        Json run = counter_token::venue_run(date_dir, mic);
        std::vector<Json> recorded;
        for (const char* k : {"inputs", "outputs"})
            if (truthy(jget(run, k)))
                for (const auto& item : run[k]) recorded.push_back(item);
        if (!recorded.empty()) {
            auto base = paths::data_root() / date_dir;
            std::vector<std::string> missing_files, changed;
            for (const auto& item : recorded) {
                std::string p = jstr(jget(item, "path"), "?");
                auto full = base / p;
                if (!std::filesystem::exists(full, ec)) {
                    missing_files.push_back(p);
                    continue;
                }
                if (counter_token::sha256_of(full) != jstr(jget(item, "sha256"), "")) changed.push_back(p);
            }
            std::vector<std::string> mparts, cparts;
            for (const auto& m : missing_files) mparts.push_back(m + " is named by the manifest but absent");
            for (const auto& m : changed) cparts.push_back(m + " no longer matches its recorded sha256");
            checks.push_back({date_dir, mic, "recorded on disk", missing_files.empty(),
                              mparts.empty() ? std::to_string(recorded.size()) + " file(s) named by the manifest are present" : util::join(mparts, "; ")});
            checks.push_back({date_dir, mic, "recorded unchanged", changed.empty(),
                              cparts.empty() ? std::to_string(recorded.size() - missing_files.size()) + " file(s) still hash to what was recorded"
                                             : util::join(cparts, "; ")});
            std::map<std::string, std::int64_t> rows;
            if (truthy(jget(run, "outputs")))
                for (const auto& o : run["outputs"])
                    if (truthy(jget(o, "rows"))) rows[jstr(jget(o, "path"))] = jint(o["rows"]);
            if (!rows.empty()) {
                std::vector<std::string> wrong_rows;
                std::int64_t sum = 0;
                for (const auto& [name, claimed] : rows) {
                    sum += claimed;
                    auto full = base / name;
                    if (!std::filesystem::exists(full, ec)) continue;
                    try {
                        auto actual = parquet::row_count(full);
                        if (actual != claimed) wrong_rows.push_back(name + " holds " + c(actual) + ", manifest says " + c(claimed));
                    } catch (const std::exception& exc) {
                        wrong_rows.push_back(name + " unreadable (" + exc.what() + ")");
                    }
                }
                checks.push_back({date_dir, mic, "recorded rows", wrong_rows.empty(),
                                  wrong_rows.empty() ? c(sum) + " row(s) recorded and counted" : util::join(wrong_rows, "; ")});
            }
        }
    }
    if (std::filesystem::is_directory(pdir, ec)) {
        std::vector<std::filesystem::path> files;
        for (const auto& p : util::list_dir_sorted(pdir)) {
            std::string n = p.filename().string();
            if (util::ends_with(n, ".parquet") && wanted.count(util::upper(n.substr(0, n.find('-')))))
                files.push_back(p);
        }
        if (!files.empty()) {
            std::set<std::string> tokens;
            std::size_t rows = 0;
            for (const auto& f : files)
                for (const auto& row : parquet::read_table(f, {"token"}).rows) {
                    ++rows;
                    tokens.insert(row[0].value_or("\x01null"));
                }
            checks.push_back({date_dir, "*", "pg key unique", tokens.size() == rows,
                              cz(rows) + " row(s) across " + std::to_string(files.size()) + " venue file(s), " + cz(tokens.size()) +
                                  " distinct token(s) -- (token, trade_date) is the primary key the push upserts on",
                              true, V2});
        }
    }
    return checks;
}

}  // namespace

std::string Check::status() const { return ok ? "PASS" : (hard ? "FAIL" : "WARN"); }

std::string Check::line() const {
    return "[" + tag + "] " + pad(status(), 4) + " " + pad(day, 21) + " " + pad(venue, 5) + " " + pad(name, 18) + " " + detail;
}

std::string tally(const std::vector<Check>& checks) {
    std::size_t failed = 0, warned = 0;
    for (const auto& ch : checks) {
        if (!ch.ok && ch.hard) ++failed;
        if (!ch.ok && !ch.hard) ++warned;
    }
    return std::to_string(checks.size()) + " check(s): " + std::to_string(checks.size() - failed - warned) + " pass, " +
           std::to_string(failed) + " fail, " + std::to_string(warned) + " warn";
}

std::vector<std::filesystem::path> write_reports(const std::vector<Check>& checks, const std::string& suite) {
    auto dir = paths::reports_dir();
    std::filesystem::create_directories(dir);
    std::string stamp = util::utc_format_ns(util::unix_now_ns(), "%Y-%m-%d %H:%M:%SZ");
    std::vector<std::filesystem::path> written;
    for (const char* tag : {V2, ALL}) {
        std::vector<Check> subset;
        for (const auto& ch : checks)
            if (std::string(tag) == ALL || ch.tag == tag) subset.push_back(ch);
        std::vector<std::string> lines;
        for (const auto& ch : subset) lines.push_back(ch.line());
        auto path = dir / (suite + "." + tag + ".txt");
        util::write_file(path, "# " + suite + " [" + tag + "]  " + stamp + "\n# " + tally(subset) + "\n" +
                                   (lines.empty() ? std::string("# no checks carried this tag\n") : util::join(lines, "\n") + "\n"));
        written.push_back(path);
    }
    return written;
}

int report(const std::vector<Check>& checks, const std::string& suite) {
    std::pair<std::string, std::string> scope;
    bool first = true;
    for (const auto& ch : checks) {
        if (first || std::pair{ch.day, ch.venue} != scope) {
            scope = {ch.day, ch.venue};
            first = false;
            log::out("\n" + ch.day + "  " + ch.venue);
        }
        log::out("  [" + ch.tag + "] [" + ch.status() + "] " + pad(ch.name, 18) + " " + ch.detail);
    }
    std::vector<Check> failed;
    for (const auto& ch : checks)
        if (!ch.ok && ch.hard) failed.push_back(ch);
    log::out("\n" + tally(checks));
    for (const auto& ch : failed) log::out("  FAIL " + ch.day + " " + ch.venue + " " + ch.name + ": " + ch.detail);
    if (!suite.empty() && !checks.empty())
        for (const auto& p : write_reports(checks, suite)) log::out("  wrote " + p.string());
    return failed.empty() ? 0 : 1;
}

std::vector<Check> state_checks() {
    std::vector<Check> checks;
    std::optional<state::Head> head;
    try {
        head = state::read_head();
    } catch (const state::StateCorrupt& exc) {
        return {{"state", "*", "head readable", false, exc.what()}};
    }
    if (!head) return {{"state", "*", "head readable", false, "no head at " + state::head_path().string() + " -- run init-state"}};
    checks.push_back({"state", "*", "head readable", true,
                      "counter " + c(head->counter) + ", " + std::to_string(head->venues.size()) + " venue(s), last run " +
                          jstr(jget(head->last_run, "run_id"), "-")});
    std::map<std::int64_t, std::string> owners;
    std::int64_t shared = 0, highest_held = 0;
    for (const auto& [mic, venue] : head->venues) {
        numbering::Holdings holdings;
        try {
            holdings = state::load_snapshot(mic, venue);
        } catch (const state::StateCorrupt& exc) {
            checks.push_back({"state", mic, "snapshot", false, exc.what(), true, V2});
            continue;
        }
        checks.push_back({"state", mic, "snapshot", true,
                          venue.snapshot + ": " + cz(holdings.assigned.size()) + " held, " + cz(holdings.free.size()) + " free",
                          true, V2});
        std::vector<std::int64_t> tokens;
        for (const auto& [s, t] : holdings.assigned) tokens.push_back(t);
        tokens.insert(tokens.end(), holdings.free.begin(), holdings.free.end());
        for (auto t : tokens)
            if (owners.emplace(t, mic).first->second != mic) ++shared;
        highest_held = std::max(highest_held, holdings.highest());
        auto days = state::venue_days(mic);
        std::string newest = days.empty() ? "" : days.back();
        bool ok = newest <= venue.newest;
        checks.push_back({"state", mic, "newest on disk", ok,
                          "state newest " + (venue.newest.empty() ? std::string("-") : venue.newest) + " (" +
                              (venue.newest_mode.empty() ? std::string("-") : venue.newest_mode) + "), newest on disk " +
                              (newest.empty() ? std::string("-") : newest) +
                              (ok ? "" : " -- a day was numbered outside a numbering session")});
    }
    checks.push_back({"state", "*", "venues disjoint", shared == 0, c(shared) + " token(s) held by more than one venue", true, V2});
    auto [on_disk, where] = counter_token::highest_issued();
    std::int64_t top = std::max(on_disk, highest_held);
    checks.push_back({"state", "*", "counter covers", head->counter >= top,
                      "head counter " + c(head->counter) + "; highest on disk " + c(on_disk) + " (" +
                          (where.empty() ? std::string("-") : where) + "), highest held " + c(highest_held),
                      true, V2});
    auto root = paths::data_root();
    std::vector<std::string> staged;
    std::error_code ec;
    if (std::filesystem::is_directory(root, ec))
        for (auto it = std::filesystem::recursive_directory_iterator(root, ec); !ec && it != std::filesystem::recursive_directory_iterator(); it.increment(ec))
            if (util::ends_with(it->path().filename().string(), state::STAGED)) staged.push_back(util::relative_to(it->path(), root));
    std::sort(staged.begin(), staged.end());
    bool header_ok = true;
    const Json& commit = head->last_commit;
    if (truthy(jget(commit, "header"))) {
        Json on_disk_header = counter_token::read_json(counter_token::venue_manifest_path(jstr(commit["date"]), jstr(commit["mic"])));
        header_ok = jget(jget(on_disk_header, "numbering"), "run_id") == commit["run_id"];
    }
    checks.push_back({"state", "*", "commit finished", staged.empty() && header_ok,
                      (staged.empty() ? std::string("no staged files; ")
                                      : std::to_string(staged.size()) + " staged file(s), e.g. " + staged[0] + "; ") +
                          (header_ok ? std::string("last commit's header written")
                                     : "last commit " + jstr(jget(commit, "run_id"), "None") +
                                           " has no header -- the next run's recovery writes it")});
    auto events = state::read_events();
    std::set<std::string> ended;
    for (const auto& e : events) {
        std::string type = jstr(e.at("eventType"));
        if (type == "COMPLETE" || type == "FAIL") ended.insert(jstr(e.at("run").at("runId")));
    }
    std::vector<std::string> open_runs;
    for (const auto& e : events)
        if (jstr(e.at("eventType")) == "START" && !ended.count(jstr(e.at("run").at("runId"))))
            open_runs.push_back(jstr(e.at("run").at("runId")));
    checks.push_back({"state", "*", "runs closed", open_runs.empty(),
                      cz(events.size()) + " event(s) in the run log" +
                          (open_runs.empty() ? "" : "; " + std::to_string(open_runs.size()) + " run(s) never closed, e.g. " +
                                                        open_runs[0] + " -- running now, or interrupted (the next run's recovery closes it)"),
                      false});
    for (auto& ch : checks)
        if (ch.tag != V2) ch.tag = ALL;
    return checks;
}

std::vector<Check> token_checks(const std::vector<std::string>& dates, const std::string& venue) {
    std::set<std::string> wanted = {util::upper(venue)};
    std::set<std::string> days(dates.begin(), dates.end());
    std::vector<Check> checks;
    for (const auto& day : days)
        for (auto& ch : token_day(day, wanted)) checks.push_back(ch);
    std::map<std::string, std::vector<std::string>> per_venue;
    for (const auto& day : days)
        for (const auto& [mic, files] : venue_files(day))
            if (wanted.count(mic)) per_venue[mic].push_back(day);
    for (const auto& [mic, ds] : per_venue)
        for (std::size_t i = 0; i + 1 < ds.size(); ++i)
            for (auto& ch : token_pair(ds[i], ds[i + 1], mic)) checks.push_back(ch);
    return checks;
}

std::vector<Check> lineage_checks(const std::vector<std::string>& dates, const std::string& venue) {
    std::set<std::string> wanted = {util::upper(venue)};
    std::vector<Check> checks;
    for (const auto& day : std::set<std::string>(dates.begin(), dates.end()))
        for (auto& ch : lineage_day(day, wanted)) checks.push_back(ch);
    return checks;
}

}  // namespace premarket::qa
