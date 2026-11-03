#include "premarket/normalize/counter_token.hpp"

#include <algorithm>
#include <system_error>

#include "premarket/log.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"

#ifndef PREMARKET_BUILD_SHA
#define PREMARKET_BUILD_SHA "unknown"
#endif

namespace premarket::counter_token {

std::string assign(std::int64_t n) {
    if (n < FIRST_TOKEN)
        throw ValueError("token " + std::to_string(n) + " is below the first issuable token " +
                         std::to_string(FIRST_TOKEN));
    if (n > INT32_MAX_TOKEN)
        throw ValueError("counterToken exhausted: " + util::commas(n) + " crosses int32 (" +
                         util::commas(INT32_MAX_TOKEN) + "). Do not wrap -- the token would collide with a live one.");
    return std::to_string(n);
}

std::int64_t capacity() { return INT32_MAX_TOKEN - FIRST_TOKEN + 1; }

void check_capacity(const std::string& mic, std::int64_t issued, std::int64_t arriving) {
    std::int64_t highest = issued + arriving;
    if (highest > INT32_MAX_TOKEN)
        throw ValueError(mic + ": " + util::commas(arriving) + " new instrument(s) would take the shared sequence to " +
                         util::commas(highest) + ", past int32 (" + util::commas(INT32_MAX_TOKEN) +
                         "). Do not wrap -- the tokens would collide with live ones.");
}

std::map<std::string, std::vector<std::string>> validate(const std::map<std::string, config::Venue>& exchanges) {
    std::map<std::string, std::vector<std::string>> errors;
    std::map<std::int64_t, std::string> taken;
    for (const auto& [venue, cfg] : exchanges) {
        const std::string& mic = cfg.mic;
        std::int64_t vid = cfg.venue_id;
        if (!vid) {
            errors[mic].push_back("venue_id is unset");
            continue;
        }
        auto owner = taken.find(vid);
        if (owner != taken.end() && !owner->second.empty() && owner->second != mic)
            errors[mic].push_back("venue_id " + std::to_string(vid) + " already used by " + owner->second +
                                  "; ids must be unique");
        else
            taken[vid] = mic;
    }
    return errors;
}

std::string VenueTokens::token(const std::string& script) const {
    auto it = assigned.find(script);
    return it == assigned.end() ? std::string() : assign(it->second);
}

std::int64_t VenueTokens::highest() const {
    std::int64_t best = 0;
    for (const auto& [s, t] : assigned) best = std::max(best, t);
    for (const auto& [s, t] : retained) best = std::max(best, t);
    return best;
}

std::int64_t Sequence::take() {
    if (issued_ + 1 > INT32_MAX_TOKEN)
        throw ValueError("counterToken exhausted: the shared sequence has issued " + util::commas(issued_) +
                         " and the next would cross int32 (" + util::commas(INT32_MAX_TOKEN) +
                         "). Do not wrap -- the token would collide.");
    return ++issued_;
}

namespace {

std::vector<std::string> present_scripts(const std::vector<std::string>& scripts) {
    std::set<std::string> set;
    for (const auto& s : scripts)
        if (!s.empty()) set.insert(s);
    return {set.begin(), set.end()};
}

}  // namespace

VenueTokens carry_forward(const VenueTokens* previous, const std::vector<std::string>& scripts,
                          std::int64_t venue_id, Sequence& sequence, const TokenMap* prefer) {
    auto present = present_scripts(scripts);
    VenueTokens out;
    out.venue_id = venue_id;
    if (previous == nullptr) {
        for (const auto& s : present) out.assigned[s] = sequence.take();
        return out;
    }
    TokenMap kept;
    for (const auto& s : present) {
        auto it = previous->assigned.find(s);
        if (it != previous->assigned.end()) kept[s] = it->second;
    }
    std::set<std::int64_t> pool_set(previous->free.begin(), previous->free.end());
    for (const auto& [s, t] : previous->assigned)
        if (!kept.count(s)) pool_set.insert(t);
    std::vector<std::int64_t> pool(pool_set.begin(), pool_set.end());

    TokenMap assigned = kept;
    std::vector<std::string> arrivals;
    for (const auto& s : present)
        if (!assigned.count(s)) arrivals.push_back(s);
    if (prefer && !prefer->empty()) {
        std::set<std::int64_t> available(pool.begin(), pool.end());
        for (const auto& script : arrivals) {
            auto it = prefer->find(script);
            if (it != prefer->end() && available.count(it->second)) {
                assigned[script] = it->second;
                available.erase(it->second);
            }
        }
        std::vector<std::int64_t> left;
        for (auto t : pool)
            if (available.count(t)) left.push_back(t);
        pool = std::move(left);
    }
    std::size_t taken = 0;
    for (const auto& script : arrivals) {
        if (assigned.count(script)) continue;
        if (taken < pool.size()) assigned[script] = pool[taken++];
        else assigned[script] = sequence.take();
    }
    out.assigned = std::move(assigned);
    out.free.assign(pool.begin() + static_cast<std::ptrdiff_t>(taken), pool.end());
    return out;
}

fs::path manifests_dir(const std::string& as_of) { return paths::day_dir(as_of) / "manifests"; }
fs::path venue_manifest_path(const std::string& as_of, const std::string& mic) {
    return manifests_dir(as_of) / (util::upper(mic) + ".json");
}
fs::path alloc_path(const std::string& as_of, const std::string& mic) {
    return manifests_dir(as_of) / (util::upper(mic) + ALLOC_SUFFIX);
}
fs::path exceptions_path(const std::string& as_of, const std::string& mic) {
    return manifests_dir(as_of) / (util::upper(mic) + EXCEPTIONS_SUFFIX);
}
fs::path sequence_path(const std::string& as_of) { return manifests_dir(as_of) / "_sequence.json"; }

Json read_json(const fs::path& path) {
    std::error_code ec;
    if (!fs::exists(path, ec)) return Json::object();
    try {
        return jsonio::read(path);
    } catch (const std::exception& exc) {
        log::out("      manifest " + path.string() + " unreadable (" + exc.what() + ") -- treating as absent");
        return Json::object();
    }
}

std::string sha256_of(const fs::path& path) { return util::sha256_file(path); }

std::string build_sha() {
    std::string stamped = util::strip(util::env("PREMARKET_BUILD_SHA"));
    if (!stamped.empty()) return stamped;
    std::string compiled = PREMARKET_BUILD_SHA;
    return compiled.empty() ? "unknown" : compiled;
}

std::int64_t jint(const Json& v, std::int64_t fallback) {
    if (v.is_number_integer() || v.is_number_unsigned()) return v.get<std::int64_t>();
    if (v.is_number_float()) return static_cast<std::int64_t>(v.get<double>());
    if (v.is_boolean()) return v.get<bool>() ? 1 : 0;
    if (v.is_string()) {
        auto n = util::parse_int(v.get<std::string>());
        if (!n) throw ValueError("invalid literal for int() with base 10: " + util::quoted(v.get<std::string>()));
        return *n;
    }
    if (v.is_null()) return fallback;
    throw ValueError("int() argument must be a string or a number");
}

std::string jstr(const Json& v, const std::string& fallback) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return fallback;
    if (v.is_number_integer() || v.is_number_unsigned()) return std::to_string(v.get<std::int64_t>());
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    return jsonio::line(v);
}

const Json& jget(const Json& obj, const std::string& key) {
    static const Json null_value;
    if (!obj.is_object()) return null_value;
    auto it = obj.find(key);
    return it == obj.end() ? null_value : *it;
}

bool truthy(const Json& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number_integer() || v.is_number_unsigned()) return v.get<std::int64_t>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<std::string>().empty();
    return !v.empty();
}

fs::path write_alloc(const fs::path& path, const VenueTokens& tokens) {
    std::set<std::int64_t> free(tokens.free.begin(), tokens.free.end());
    std::vector<parquet::NullableRow> rows;
    rows.reserve(tokens.assigned.size() + tokens.retained.size() + free.size());
    for (const auto& [s, t] : tokens.assigned) rows.push_back({s, std::to_string(t), std::string(ALLOC_ASSIGNED)});
    for (const auto& [s, t] : tokens.retained) rows.push_back({s, std::to_string(t), std::string(ALLOC_RETAINED)});
    for (auto t : free) rows.push_back({std::nullopt, std::to_string(t), std::string(ALLOC_FREE)});
    fs::create_directories(path.parent_path());
    auto staging = util::staging_path(path);
    parquet::write_table(staging,
                         {{"script", parquet::Type::String}, {"token", parquet::Type::Int32}, {"state", parquet::Type::String}},
                         rows);
    util::replace_file(staging, path);
    return path;
}

namespace {

std::string repr_or_none(const parquet::Cell& c) { return c ? util::quoted(*c) : "None"; }

}  // namespace

Allocation read_allocation(const fs::path& path) {
    auto table = parquet::read_table(path, {"script", "token", "state"});
    Allocation out;
    std::string name = path.filename().string();
    for (const auto& row : table.rows) {
        const auto& script = row[0];
        std::string token = row[1].value_or("None");
        const auto& state = row[2];
        bool has_script = script && !script->empty();
        if (state && *state == ALLOC_FREE) {
            if (has_script)
                throw ManifestCorrupt(name + ": token " + token + " is free but names script " + util::quoted(*script));
            out.free.push_back(std::stoll(token));
        } else if (state && (*state == ALLOC_ASSIGNED || *state == ALLOC_RETAINED)) {
            if (!has_script) throw ManifestCorrupt(name + ": token " + token + " is " + *state + " but names no script");
            (*state == ALLOC_ASSIGNED ? out.assigned : out.retained)[*script] = std::stoll(token);
        } else {
            throw ManifestCorrupt(name + ": token " + token + " has unknown state " + repr_or_none(state));
        }
    }
    return out;
}

std::pair<TokenMap, std::vector<std::int64_t>> read_alloc(const fs::path& path) {
    auto a = read_allocation(path);
    if (!a.retained.empty())
        throw ManifestCorrupt(path.filename().string() + ": " + util::commas(static_cast<std::int64_t>(a.retained.size())) +
                              " retained token(s); read it with read_allocation, which keeps them");
    return {a.assigned, a.free};
}

void require_current(const Json& doc, const fs::path& path) {
    const Json& v = jget(doc, "version");
    std::int64_t version = v.is_null() ? 0 : jint(v);
    if (version == MANIFEST_VERSION) return;
    if (version < MANIFEST_VERSION)
        throw ManifestCorrupt(path.string() + " is a 7.x manifest (version " + std::to_string(version) +
                              "). Run `premarket migrate-state --reason \"...\"` once to move this data tree to v8.");
    throw ManifestCorrupt(path.string() + " is manifest version " + std::to_string(version) + "; this build reads " +
                          std::to_string(MANIFEST_VERSION));
}

std::optional<VenueEntry> venue_entry(const std::string& as_of, const std::string& mic_in, bool any_version) {
    std::string mic = util::upper(mic_in);
    auto header_path = venue_manifest_path(as_of, mic);
    Json doc = read_json(header_path);
    if (!truthy(doc)) return std::nullopt;
    if (!any_version) require_current(doc, header_path);
    const Json& block_raw = jget(doc, "allocation");
    Json block = truthy(block_raw) ? block_raw : Json::object();
    auto path = alloc_path(as_of, mic);
    std::error_code ec;
    if (!fs::exists(path, ec))
        throw ManifestCorrupt(mic + ": " + as_of + " has a header but no allocation table (" + path.filename().string() +
                              "). Refusing to read this as an unnumbered venue -- that would renumber it from scratch "
                              "and hand its live tokens to other instruments.");
    std::string recorded = jstr(jget(block, "sha256"), "");
    std::string actual = sha256_of(path);
    if (!recorded.empty() && recorded != actual)
        throw ManifestCorrupt(mic + ": " + as_of + "'s allocation table does not match its header -- sha256 is " + actual +
                              " on disk but the header records " + recorded +
                              ". The table changed after the manifest was written.");
    auto a = read_allocation(path);
    VenueEntry e;
    e.venue_id = jint(jget(block, "venue_id"), 0);
    e.highest = jint(jget(block, "highest"), 0);
    e.count = block.contains("count") ? jint(block["count"], 0) : static_cast<std::int64_t>(a.assigned.size());
    e.assigned = std::move(a.assigned);
    e.free = std::move(a.free);
    e.retained = std::move(a.retained);
    return e;
}

std::set<std::string> venues_with_manifest(const std::string& as_of) {
    std::set<std::string> out;
    auto dir = manifests_dir(as_of);
    std::error_code ec;
    if (!fs::is_directory(dir, ec)) return out;
    for (const auto& p : util::list_dir_sorted(dir)) {
        std::string name = p.filename().string();
        if (!util::ends_with(name, ".json") || util::starts_with(name, "_")) continue;
        out.insert(util::upper(name.substr(0, name.size() - 5)));
    }
    return out;
}

Json venue_run(const std::string& as_of, const std::string& mic) {
    Json doc = read_json(venue_manifest_path(as_of, util::upper(mic)));
    Json run = Json::object();
    if (!truthy(doc)) return run;
    for (const char* k : {"started_at", "completed_at"})
        if (doc.contains(k)) run[k] = doc[k];
    const Json& code = jget(doc, "code");
    if (truthy(code)) run["build_sha"] = jget(code, "build_sha").is_null() ? Json("") : code["build_sha"];
    if (truthy(jget(doc, "tokens"))) run["tokens"] = doc["tokens"];
    for (const char* k : {"inputs", "outputs"})
        if (truthy(jget(doc, k))) run[k] = doc[k];
    return run;
}

std::string utc_now() { return util::utc_now_iso(); }

Json Artifact::as_dict() const {
    Json out = {{"path", path}, {"bytes", bytes}, {"sha256", sha256}};
    if (rows) out["rows"] = rows;
    return out;
}

Artifact artifact(const fs::path& path, const std::string& as_of, std::int64_t rows) {
    fs::path base = paths::data_root() / as_of;
    std::string name;
    try {
        name = util::relative_to(path, base);
    } catch (const ValueError&) {
        name = path.filename().string();
    }
    return Artifact{name, static_cast<std::int64_t>(fs::file_size(path)), sha256_of(path), rows};
}

Json RunStats::as_dict() const {
    Json d = Json::object();
    for (const auto& [k, v] : day) d[k] = v;
    return {{"day", d},
            {"carried_from", carried_from},
            {"run",
             {{"drawn", drawn},
              {"sequence_before", sequence_before},
              {"sequence_after", sequence_after},
              {"anchored_on", anchored_on},
              {"sequence_from", sequence_from}}}};
}

std::map<std::string, std::int64_t> day_stats(const VenueTokens* previous_day, const VenueTokens& tokens,
                                              std::int64_t previous_issued) {
    auto n = static_cast<std::int64_t>(tokens.assigned.size());
    if (previous_day == nullptr)
        return {{"kept", 0}, {"arrived", n}, {"departed", 0}, {"released", 0}, {"reused", 0}, {"drawn", n}};
    std::int64_t kept = 0, arrived = 0, drawn = 0;
    for (const auto& [script, token] : tokens.assigned) {
        if (previous_day->assigned.count(script)) {
            ++kept;
        } else {
            ++arrived;
            if (token > previous_issued) ++drawn;
        }
    }
    std::int64_t departed = static_cast<std::int64_t>(previous_day->assigned.size()) - kept;
    return {{"kept", kept},         {"arrived", arrived},         {"departed", departed},
            {"released", departed}, {"reused", arrived - drawn}, {"drawn", drawn}};
}

RunStats run_stats(const std::string& as_of, const std::string& mic, std::int64_t venue_id, const VenueTokens& tokens,
                   const Sequence* sequence, const std::string& anchored_on, const std::string& sequence_from) {
    std::int64_t before = sequence ? sequence->start() : 0;
    std::int64_t after = sequence ? sequence->issued() : 0;
    std::optional<VenueTokens> previous_day;
    std::string carried_from;
    std::int64_t previous_issued = 0;
    try {
        auto [prev, stamp] = previous_tokens(as_of, mic, venue_id);
        previous_day = prev;
        carried_from = stamp;
        auto [issued, s] = previous_sequence(as_of);
        previous_issued = issued.value_or(0);
    } catch (const std::exception& exc) {
        log::out("      run record: could not read " + mic + "'s previous day (" + exc.what() +
                 "); the day block will describe a first day");
        previous_day.reset();
        carried_from.clear();
        previous_issued = 0;
    }
    RunStats r;
    r.day = day_stats(previous_day ? &*previous_day : nullptr, tokens, previous_issued);
    r.carried_from = carried_from;
    r.drawn = after - before;
    r.sequence_before = before;
    r.sequence_after = after;
    r.anchored_on = anchored_on;
    r.sequence_from = sequence_from;
    return r;
}

Json header_payload(const std::string& as_of, const std::string& mic, const VenueTokens& tokens,
                    const std::string& alloc_name, std::int64_t alloc_bytes, const std::string& alloc_sha,
                    const std::string& started_at, const RunStats* run, const std::vector<Artifact>& inputs,
                    const std::vector<Artifact>& outputs, const Json* numbering) {
    std::string completed_at = utc_now();
    std::set<std::int64_t> free(tokens.free.begin(), tokens.free.end());
    Json in = Json::array(), out = Json::array();
    for (const auto& a : inputs) in.push_back(a.as_dict());
    for (const auto& a : outputs) out.push_back(a.as_dict());
    auto assigned = static_cast<std::int64_t>(tokens.assigned.size());
    auto retained = static_cast<std::int64_t>(tokens.retained.size());
    auto free_n = static_cast<std::int64_t>(free.size());
    Json payload = {
        {"version", MANIFEST_VERSION},
        {"date", as_of},
        {"venue", util::upper(mic)},
        {"started_at", started_at.empty() ? completed_at : started_at},
        {"completed_at", completed_at},
        {"code", {{"build_sha", build_sha()}, {"manifest_version", MANIFEST_VERSION}}},
        {"allocation",
         {{"venue_id", tokens.venue_id},
          {"highest", tokens.highest()},
          {"count", assigned},
          {"free_count", free_n},
          {"retained_count", retained},
          {"path", alloc_name},
          {"rows", assigned + retained + free_n},
          {"bytes", alloc_bytes},
          {"sha256", alloc_sha}}},
        {"tokens", (run ? *run : RunStats{}).as_dict()},
        {"inputs", in},
        {"outputs", out},
    };
    if (numbering) payload["numbering"] = *numbering;
    return payload;
}

fs::path write_header(const std::string& as_of, const std::string& mic, const Json& payload) {
    auto path = venue_manifest_path(as_of, mic);
    fs::create_directories(path.parent_path());
    auto staging = util::staging_path(path);
    util::write_file(staging, jsonio::pretty(payload));
    util::replace_file(staging, path);
    return path;
}

fs::path write_venue_manifest(const std::string& as_of, const std::string& mic_in, const VenueTokens& tokens,
                              const std::string& started_at, const RunStats* run, const std::vector<Artifact>& inputs,
                              const std::vector<Artifact>& outputs, const Json* numbering) {
    std::string mic = util::upper(mic_in);
    auto alloc = write_alloc(alloc_path(as_of, mic), tokens);
    auto payload = header_payload(as_of, mic, tokens, alloc.filename().string(),
                                  static_cast<std::int64_t>(fs::file_size(alloc)), sha256_of(alloc), started_at, run,
                                  inputs, outputs, numbering);
    return write_header(as_of, mic, payload);
}

fs::path write_exceptions(const fs::path& path, std::vector<ExceptionRow> rows) {
    std::sort(rows.begin(), rows.end(), [](const ExceptionRow& a, const ExceptionRow& b) {
        return std::tie(a.script, a.wanted_from) < std::tie(b.script, b.wanted_from);
    });
    std::vector<parquet::NullableRow> table;
    for (const auto& r : rows)
        table.push_back({r.script, std::to_string(r.token), std::to_string(r.wanted), r.wanted_from, r.lost_to});
    fs::create_directories(path.parent_path());
    auto staging = util::staging_path(path);
    parquet::write_table(staging,
                         {{"script", parquet::Type::String},
                          {"token", parquet::Type::Int32},
                          {"wanted", parquet::Type::Int32},
                          {"wanted_from", parquet::Type::String},
                          {"lost_to", parquet::Type::String}},
                         table);
    util::replace_file(staging, path);
    return path;
}

std::vector<ExceptionRow> read_exceptions(const std::string& as_of, const std::string& mic) {
    Json header = read_json(venue_manifest_path(as_of, util::upper(mic)));
    const Json& numbering = jget(header, "numbering");
    const Json& block = jget(numbering, "exceptions");
    if (!truthy(jget(block, "count"))) return {};
    auto path = manifests_dir(as_of) / jstr(jget(block, "path"));
    std::error_code ec;
    if (!fs::exists(path, ec) || sha256_of(path) != jstr(jget(block, "sha256")))
        throw ManifestCorrupt(mic + ": " + as_of + "'s exceptions table is missing or does not match its header");
    auto table = parquet::read_table(path);
    std::vector<ExceptionRow> out;
    int is = table.index("script"), it = table.index("token"), iw = table.index("wanted"),
        iwf = table.index("wanted_from"), il = table.index("lost_to");
    for (const auto& row : table.rows) {
        ExceptionRow r;
        r.script = row[static_cast<std::size_t>(is)].value_or("");
        r.token = std::stoll(row[static_cast<std::size_t>(it)].value_or("0"));
        r.wanted = std::stoll(row[static_cast<std::size_t>(iw)].value_or("0"));
        r.wanted_from = row[static_cast<std::size_t>(iwf)].value_or("");
        r.lost_to = row[static_cast<std::size_t>(il)].value_or("");
        out.push_back(r);
    }
    return out;
}

std::optional<std::int64_t> load_sequence(const std::string& as_of) {
    Json doc = read_json(sequence_path(as_of));
    if (!doc.is_object() || !doc.contains("issued")) return std::nullopt;
    require_current(doc, sequence_path(as_of));
    return jint(doc["issued"]);
}

std::pair<std::optional<std::int64_t>, std::string> previous_sequence(const std::string& as_of) {
    for (int back = 1; back <= MANIFEST_LOOKBACK_DAYS; ++back) {
        std::string stamp = util::add_days(as_of, -back);
        if (auto issued = load_sequence(stamp)) return {issued, stamp};
    }
    return {std::nullopt, ""};
}

std::vector<std::string> numbered_day_dirs() {
    std::vector<std::string> out;
    auto root = paths::data_root();
    std::error_code ec;
    if (!fs::is_directory(root, ec)) return out;
    for (const auto& child : util::list_dir_sorted(root)) {
        std::string name = child.filename().string();
        if (name.size() == 8 && util::is_digits(name) && fs::is_directory(manifests_dir(name), ec)) out.push_back(name);
    }
    return out;
}

std::pair<std::int64_t, std::string> highest_issued() {
    std::int64_t best = FIRST_TOKEN - 1;
    std::string where;
    for (const auto& day : numbered_day_dirs()) {
        auto issued = load_sequence(day);
        if (issued && *issued > best) {
            best = *issued;
            where = day;
        }
        for (const auto& header : util::list_dir_sorted(manifests_dir(day))) {
            std::string name = header.filename().string();
            if (!util::ends_with(name, ".json") || util::starts_with(name, "_")) continue;
            const Json& alloc = jget(read_json(header), "allocation");
            const Json& h = jget(alloc, "highest");
            std::int64_t highest = truthy(h) ? jint(h) : 0;
            if (highest > best) {
                best = highest;
                where = day;
            }
        }
    }
    return {best, where};
}

std::pair<Sequence, std::string> open_sequence(const std::string& as_of) {
    std::int64_t issued;
    std::string stamp;
    if (auto today = load_sequence(as_of)) {
        issued = *today;
        stamp = as_of;
    } else {
        auto [previous, previous_stamp] = previous_sequence(as_of);
        if (previous) {
            issued = *previous;
            stamp = previous_stamp;
        } else {
            issued = FIRST_TOKEN - 1;
            stamp = "";
        }
    }
    auto [highest, where] = highest_issued();
    if (highest > issued) return {Sequence(highest), where};
    return {Sequence(issued), stamp};
}

fs::path write_sequence(const std::string& as_of, const Sequence& sequence) {
    auto path = sequence_path(as_of);
    fs::create_directories(path.parent_path());
    auto staging = util::staging_path(path);
    Json doc = {{"version", MANIFEST_VERSION}, {"date", as_of}, {"issued", sequence.issued()}};
    util::write_file(staging, jsonio::line(doc));
    util::replace_file(staging, path);
    return path;
}

namespace {

VenueTokens tokens_from(const VenueEntry& e) {
    VenueTokens t;
    t.venue_id = e.venue_id;
    t.assigned = e.assigned;
    t.free = e.free;
    t.retained = e.retained;
    return t;
}

}  // namespace

std::pair<std::optional<VenueTokens>, std::string> opening_tokens(const std::string& as_of, const std::string& mic,
                                                                  std::int64_t venue_id) {
    if (auto entry = venue_entry(as_of, mic)) {
        if (entry->venue_id != venue_id)
            throw ValueError(mic + ": venue_id is " + std::to_string(venue_id) + " in config.ini but " +
                             std::to_string(entry->venue_id) + " in " + as_of +
                             "'s own manifest, written earlier today. Refusing to continue a run that would renumber "
                             "the venue mid-day.");
        return {tokens_from(*entry), as_of};
    }
    return previous_tokens(as_of, mic, venue_id);
}

std::pair<std::optional<VenueTokens>, std::string> previous_tokens(const std::string& as_of, const std::string& mic,
                                                                   std::int64_t venue_id) {
    for (int back = 1; back <= MANIFEST_LOOKBACK_DAYS; ++back) {
        std::string stamp = util::add_days(as_of, -back);
        auto entry = venue_entry(stamp, mic);
        if (!entry) continue;
        if (entry->venue_id != venue_id)
            throw ValueError(mic + ": venue_id is " + std::to_string(venue_id) + " in config.ini but " +
                             std::to_string(entry->venue_id) + " in " + stamp +
                             "'s manifest. The token blocks are derived from venue_id, so this moves the venue onto "
                             "a different block -- refusing to carry the old allocation forward. Restore venue_id=" +
                             std::to_string(entry->venue_id) + ", or delete the manifests to renumber from scratch.");
        return {tokens_from(*entry), stamp};
    }
    return {std::nullopt, ""};
}

const config::Venue* exchange_for(const std::string& venue) { return config::venue(venue); }

}  // namespace premarket::counter_token
