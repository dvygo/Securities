#include "premarket/load.hpp"

#include <set>

#include "premarket/clickhouse.hpp"
#include "premarket/config.hpp"
#include "premarket/export.hpp"
#include "premarket/ini.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/paths.hpp"
#include "premarket/plugin.hpp"
#include "premarket/postgres.hpp"

namespace premarket::load {

using jsonio::Json;

std::string Sink::get(const std::string& key, const std::string& fallback) const {
    auto it = settings.find(key);
    return it == settings.end() ? fallback : it->second;
}

std::string Sink::require(const std::string& key) const {
    std::string v = get(key);
    if (v.empty()) throw SinkError("sink " + name + ": `" + key + "` is not set in " + path.string());
    return v;
}

std::vector<std::string> available() {
    std::vector<std::string> out;
    auto dir = paths::sinks_dir();
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return out;
    for (const auto& p : util::list_dir_sorted(dir))
        if (util::ends_with(p.filename().string(), ".ini")) out.push_back(p.stem().string());
    return out;
}

namespace {

const std::map<std::string, std::set<std::string>> kKeys = {
    {CLICKHOUSE_NORMAL, {"type", "host", "port", "database", "username", "password", "secure"}},
    {POSTGRES_PLUGIN, {"type", "market", "venue_id", "database_url", "schema", "table", "create_table"}},
    {MDF_TOKENMAP, {"type", "markets", "dir"}},
};

std::pair<std::string, std::int64_t> plugin_market(const Sink& sink) {
    std::string market = util::upper(util::strip(sink.require("market")));
    if (market.find(',') != std::string::npos)
        throw SinkError("sink " + sink.name + ": a postgres-plugin sink pushes ONE market; make one sink file per market");
    auto vid = util::parse_int(sink.require("venue_id"));
    if (!vid) throw SinkError("sink " + sink.name + ": venue_id must be a whole number");
    const auto* v = config::venue(market);
    if (v == nullptr) throw SinkError("sink " + sink.name + ": market " + market + " is not in " + config::get().file.string());
    if (v->venue_id != *vid)
        throw SinkError("sink " + sink.name + ": venue_id " + std::to_string(*vid) + " but " + config::get().file.string() +
                        " numbers " + market + " as venue_id " + std::to_string(v->venue_id) +
                        " -- refusing to push one venue's rows under another's identity");
    if (!v->enabled) throw SinkError("sink " + sink.name + ": " + market + " is disabled (enabled = 0)");
    return {market, *vid};
}

std::string newest_live(const state::Head& head, const std::string& venue) {
    auto it = head.venues.find(venue);
    return it == head.venues.end() ? "" : it->second.live_date;
}

std::vector<std::string> tokenmap_markets(const Sink& sink) {
    std::vector<std::string> markets;
    for (const auto& m : util::split(sink.get("markets", ""), ","))
        if (!util::strip(m).empty()) markets.push_back(util::upper(util::strip(m)));
    return markets;
}

std::string not_delivered(const Sink& sink, const std::string& venue) {
    if (sink.type == POSTGRES_PLUGIN) {
        std::string market = util::upper(util::strip(sink.get("market")));
        return market == venue ? "" : "it pushes " + (market.empty() ? std::string("no market") : market);
    }
    if (sink.type == MDF_TOKENMAP) {
        const auto* v = config::venue(venue);
        if (v == nullptr || v->feed != "databento") return "MDF token maps are for Databento venues";
        auto markets = tokenmap_markets(sink);
        if (!markets.empty() && std::find(markets.begin(), markets.end(), venue) == markets.end())
            return "its markets are " + util::join(markets, ",");
    }
    return "";
}

Json normalized_inputs(const std::string& date_dir, const std::set<std::string>& markets) {
    Json out = Json::array();
    for (const auto& p : exporter::normalized_files(date_dir))
        if (markets.empty() || markets.count(util::upper(exporter::exchange_of(p))))
            out.push_back(counter_token::artifact(p, date_dir).as_dict());
    return out;
}

struct Outcome {
    Json inputs = Json::array();
    Json outputs = Json::array();
    std::optional<bool> current_updated;
};

Outcome clickhouse_normal(const std::string& date_dir, const std::string& venue, const Sink& sink, const state::Head& head,
                          bool dry_run) {
    clickhouse::Config cfg;
    cfg.host = sink.require("host");
    auto port = util::parse_int(sink.get("port", "8123"));
    if (!port) throw SinkError("sink " + sink.name + ": port must be a whole number");
    cfg.port = *port;
    cfg.database = sink.require("database");
    cfg.username = sink.get("username", "default");
    cfg.password = sink.get("password", "");
    std::string secure = sink.get("secure", "0");
    if (secure != "0" && secure != "1") throw SinkError("sink " + sink.name + ": secure must be 0 or 1, got `" + secure + "`");
    cfg.secure = secure == "1";
    std::string newest = newest_live(head, venue);
    bool current = newest.empty() || date_dir >= newest;
    if (!current)
        log::out("    " + date_dir + " is older than " + venue + "'s newest live day " + newest +
                 ": dated tables only, the current contracts/baskets mirrors are left alone");
    Outcome o;
    o.inputs = normalized_inputs(date_dir, {venue});
    if (dry_run) {
        log::out("    DRY RUN: would push " + venue + " (" + std::to_string(o.inputs.size()) + " normalized file(s)) to " +
                 cfg.host + ":" + std::to_string(cfg.port) + "/" + cfg.database + (current ? "" : " (dated tables only)"));
        return o;
    }
    auto result = clickhouse::push(cfg, date_dir, venue, current);
    for (const auto& t : result.tables)
        o.outputs.push_back({{"path", "clickhouse://" + cfg.host + "/" + t},
                             {"sha256", ""},
                             {"rows", t.find("contracts") != std::string::npos ? result.contracts : result.baskets}});
    o.current_updated = current;
    return o;
}

Outcome postgres_plugin(const std::string& date_dir, const std::string&, const Sink& sink, const state::Head&, bool dry_run) {
    auto [market, vid] = plugin_market(sink);
    postgres::Config cfg;
    cfg.database_url = sink.require("database_url");
    cfg.schema = sink.require("schema");
    cfg.table = sink.require("table");
    cfg.market = market;
    std::string create = sink.get("create_table", "0");
    if (create != "0" && create != "1") throw SinkError("sink " + sink.name + ": create_table must be 0 or 1");
    cfg.create_table = create == "1";
    Outcome o;
    o.inputs = normalized_inputs(date_dir, {market});
    if (dry_run) {
        log::out("    DRY RUN: would build the plugin file for " + market + " from " + std::to_string(o.inputs.size()) +
                 " normalized file(s) and upsert it into " + cfg.schema + "." + cfg.table);
        return o;
    }
    auto files = plugin::build_day(date_dir, {market});
    for (const auto& r : postgres::push(cfg, files))
        o.outputs.push_back({{"path", "postgres://" + cfg.schema + "." + cfg.table + "#" + r.file.filename().string()},
                             {"sha256", counter_token::sha256_of(r.file)},
                             {"rows", r.upserted}});
    return o;
}

Outcome mdf_tokenmap(const std::string& date_dir, const std::string& venue, const Sink& sink, const state::Head& head,
                     bool dry_run) {
    std::vector<std::string> markets = {venue};
    std::string delivery = util::strip(sink.get("dir", ""));
    if (!delivery.empty() && !std::filesystem::path(delivery).is_absolute())
        throw SinkError("sink " + sink.name + ": dir must be an absolute path, got `" + delivery + "`");
    std::filesystem::path out_dir = delivery.empty() ? plugin::tokenmap::tokenmap_dir(date_dir) : std::filesystem::path(delivery);
    if (!delivery.empty()) {
        std::vector<std::string> stale;
        for (const auto& m : markets) {
            auto it = head.venues.find(m);
            if (it != head.venues.end() && !it->second.live_date.empty() && date_dir < it->second.live_date) stale.push_back(m);
        }
        if (!stale.empty())
            throw SinkError("sink " + sink.name + ": refusing to deliver " + date_dir + "'s map for " + util::join(stale, ", ") +
                            " into " + out_dir.string() +
                            " -- the newest live day is newer, and MDF would load the old map as current. Load it with "
                            "a sink that writes into the day's own tree (no `dir`).");
    }
    Outcome o;
    o.inputs = normalized_inputs(date_dir, {markets.begin(), markets.end()});
    if (dry_run) {
        log::out("    DRY RUN: would write token maps for " + util::join(markets, ", ") + " into " + out_dir.string());
        return o;
    }
    for (const auto& p : plugin::tokenmap::emit(date_dir, markets, out_dir))
        o.outputs.push_back({{"path", p.string()}, {"sha256", counter_token::sha256_of(p)}, {"rows", 0}});
    return o;
}

}  // namespace

Sink load_sink(const std::string& name) {
    auto path = paths::sinks_dir() / (name + ".ini");
    std::error_code ec;
    if (!std::filesystem::exists(path, ec)) {
        auto have = available();
        throw SinkError("no sink named " + util::quoted(name) + ": " + path.string() + " does not exist (configured: " +
                        (have.empty() ? "none" : util::join(have, ", ")) + "; templates: " + paths::sinks_dir().string() +
                        "/*.ini.example)");
    }
    auto file = ini::File::read(path);
    const auto* sec = file.find("sink");
    if (sec == nullptr) throw SinkError(path.string() + " has no [sink] section");
    for (const auto* s : file.sections())
        if (s->name != "sink") throw SinkError(file.where(*s) + " is not a known section (sink)");
    Sink sink{name, util::strip(file.text(*sec, "type")), {}, path};
    if (sink.type == "postgres-normal") throw SinkError("sink " + name + ": type postgres-normal -- the canonical schema into Postgres is planned, not built");
    auto keys = kKeys.find(sink.type);
    if (keys == kKeys.end())
        throw SinkError("sink " + name + ": unknown type " + util::quoted(sink.type) + " (one of clickhouse-normal, postgres-plugin, mdf-tokenmap)");
    file.only(*sec, keys->second);
    for (const auto& k : sec->order) sink.settings[k] = sec->entries.at(k).value;
    return sink;
}

void validate(const Sink& sink) {
    if (sink.type == POSTGRES_PLUGIN) plugin_market(sink);
    if (sink.type == MDF_TOKENMAP) {
        std::string dir = util::strip(sink.get("dir", ""));
        if (!dir.empty() && !std::filesystem::path(dir).is_absolute())
            throw SinkError("sink " + sink.name + ": dir must be an absolute path, got `" + dir + "`");
    }
}

std::vector<Sink> sinks_for(const std::string& venue, const std::vector<std::string>& names_in) {
    std::vector<std::string> names;
    for (const auto& n : names_in)
        if (std::find(names.begin(), names.end(), n) == names.end()) names.push_back(n);
    std::vector<Sink> picked;
    if (names.empty()) {
        auto have = available();
        if (have.empty())
            throw SinkError("no sinks: " + paths::sinks_dir().string() + " holds no <name>.ini (templates: *.ini.example)");
        for (const auto& n : have) {
            auto sink = load_sink(n);
            if (not_delivered(sink, venue).empty()) picked.push_back(sink);
        }
        if (picked.empty())
            throw SinkError("no sink in " + paths::sinks_dir().string() + " delivers " + venue + " (sinks there: " +
                            util::join(have, ", ") + ")");
    } else {
        for (const auto& n : names) {
            auto sink = load_sink(n);
            if (auto why = not_delivered(sink, venue); !why.empty())
                throw SinkError("sink " + n + " does not deliver " + venue + ": " + why);
            picked.push_back(sink);
        }
    }
    for (const auto& sink : picked) validate(sink);
    return picked;
}

int run(const std::string& date_dir, const std::string& venue, const std::vector<std::string>& names_in, bool dry_run) {
    auto sinks = sinks_for(venue, names_in);
    std::vector<std::string> failures;
    state::Lock lock("load");
    auto head = state::require_head();
    std::string run_id = state::new_run_id(date_dir, "load");
    if (!dry_run) {
        Json sink_names = Json::array();
        for (const auto& s : sinks) sink_names.push_back(s.name);
        state::append_event(state::event("START", run_id, "load",
                                         {{"date", date_dir}, {"venue", venue}, {"sinks", sink_names}, {"operator", state::operator_id()}}));
    }
    for (const auto& sink : sinks) {
        log::out(">>> load " + sink.name + " (" + sink.type + ")");
        std::string child = state::new_run_id(date_dir, "load");
        Outcome result;
        try {
            if (sink.type == CLICKHOUSE_NORMAL) result = clickhouse_normal(date_dir, venue, sink, head, dry_run);
            else if (sink.type == POSTGRES_PLUGIN) result = postgres_plugin(date_dir, venue, sink, head, dry_run);
            else result = mdf_tokenmap(date_dir, venue, sink, head, dry_run);
        } catch (const std::exception& exc) {
            failures.push_back(sink.name + ": " + exc.what());
            log::out("error: load " + sink.name + ": " + exc.what());
            if (!dry_run)
                state::append_event(state::event("FAIL", child, "load",
                                                 {{"sink", sink.name}, {"type", sink.type}, {"date", date_dir}, {"venue", venue}, {"error", exc.what()}},
                                                 Json::array(), Json::array(), run_id));
            continue;
        }
        if (!dry_run) {
            Json facets = {{"sink", sink.name}, {"type", sink.type}, {"date", date_dir}, {"venue", venue}};
            if (result.current_updated) facets["current_updated"] = *result.current_updated;
            state::append_event(state::event("COMPLETE", child, "load", facets, result.inputs, result.outputs, run_id));
        }
    }
    if (!dry_run) {
        Json f = Json::array();
        for (const auto& x : failures) f.push_back(x);
        state::append_event(state::event(failures.empty() ? "COMPLETE" : "FAIL", run_id, "load",
                                         {{"date", date_dir}, {"venue", venue}, {"failures", f}}));
    }
    return failures.empty() ? 0 : 1;
}

}  // namespace premarket::load
