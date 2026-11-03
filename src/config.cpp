#include "premarket/config.hpp"

#include <date/tz.h>

#include <chrono>
#include <set>

#include "premarket/ini.hpp"
#include "premarket/util.hpp"

namespace premarket::config {

namespace {

const std::set<std::string> kFeeds = {"databento", "fyers", "nse", "mcx"};

struct Loaded {
    Config cfg;
    std::map<std::string, std::string> keys;
    fs::path keys_path;
};

std::optional<Loaded>& current() {
    static std::optional<Loaded> state;
    return state;
}

bool is_mic(const std::string& s) {
    if (s.size() != 4) return false;
    for (char c : s)
        if (!(c >= 'A' && c <= 'Z')) return false;
    return true;
}

}  // namespace

Config load(const fs::path& path) {
    if (!path.is_absolute()) throw ValueError("--config must be an absolute path, got `" + path.string() + "`");
    auto file = ini::File::read(path);
    Config cfg;
    cfg.file = path;

    const auto* meta = file.find("premarket");
    if (meta == nullptr)
        throw ValueError(path.string() +
                         ": no [premarket] section. This is not a premarket 8 config (7.x files used [EXCHANGE:...] "
                         "sections); write a new one from conf/config.ini.example.");
    file.only(*meta, {"config_version"});
    auto version = file.integer(*meta, "config_version");
    if (version != CONFIG_VERSION)
        throw ValueError(file.where(*meta, "config_version") + " is " + std::to_string(version) + "; this build reads " +
                         std::to_string(CONFIG_VERSION));

    const auto& paths = file.at("paths");
    file.only(paths, {"data_dir", "contracts_dir", "logs_dir", "reports_dir", "baskets_dir", "sinks_dir"});
    cfg.paths.data_dir = file.absolute_path(paths, "data_dir");
    cfg.paths.contracts_dir = file.absolute_path(paths, "contracts_dir");
    cfg.paths.logs_dir = file.absolute_path(paths, "logs_dir");
    cfg.paths.reports_dir = file.absolute_path(paths, "reports_dir");
    cfg.paths.baskets_dir = file.absolute_path(paths, "baskets_dir");
    cfg.paths.sinks_dir = file.absolute_path(paths, "sinks_dir");

    const auto& run = file.at("run");
    file.only(run, {"timezone"});
    cfg.timezone = file.required(run, "timezone");
    try {
        (void)date::locate_zone(cfg.timezone);
    } catch (const std::exception&) {
        throw ValueError(file.where(run, "timezone") + " `" + cfg.timezone + "` is not a timezone in the tz database");
    }

    if (const auto* b = file.find("baskets")) {
        file.only(*b, {"enabled"});
        cfg.baskets_enabled = file.flag(*b, "enabled", true);
    }

    if (const auto* f = file.find("fyers")) {
        file.only(*f, {"base_url", "user_agent", "timeout_sec", "retries", "retry_delay_sec"});
        cfg.fyers.base_url = file.text(*f, "base_url", cfg.fyers.base_url);
        cfg.fyers.user_agent = file.text(*f, "user_agent", cfg.fyers.user_agent);
        cfg.fyers.timeout_sec = file.integer(*f, "timeout_sec", cfg.fyers.timeout_sec);
        cfg.fyers.retries = file.integer(*f, "retries", cfg.fyers.retries);
        cfg.fyers.retry_delay_sec = file.integer(*f, "retry_delay_sec", cfg.fyers.retry_delay_sec);
        if (cfg.fyers.retries < 1) throw ValueError(file.where(*f, "retries") + " must be at least 1");
    }

    std::string prefix = VENUE_PREFIX;
    for (const auto* s : file.sections()) {
        const std::string& name = s->name;
        if (name == "premarket" || name == "paths" || name == "run" || name == "baskets" || name == "fyers") continue;
        if (!util::starts_with(name, prefix))
            throw ValueError(file.where(*s) + " is not a known section (premarket, paths, run, baskets, fyers, venue.<MIC>)");
        std::string mic = name.substr(prefix.size());
        if (!is_mic(mic)) throw ValueError(file.where(*s) + ": `" + mic + "` is not a four-letter upper-case MIC");
        file.only(*s, {"feed", "enabled", "venue_id", "dataset", "stype_in", "symbols"});
        Venue v;
        v.mic = mic;
        v.feed = file.required(*s, "feed");
        if (!kFeeds.count(v.feed))
            throw ValueError(file.where(*s, "feed") + " is `" + v.feed + "`; one of databento, fyers, nse, mcx");
        v.enabled = file.flag(*s, "enabled", true);
        v.venue_id = file.integer(*s, "venue_id", 0);
        if (v.feed == "databento") {
            v.dataset = file.required(*s, "dataset");
            v.stype_in = file.text(*s, "stype_in", "raw_symbol");
            v.symbols = file.absolute_path(*s, "symbols", false);
        } else {
            for (const char* k : {"dataset", "stype_in", "symbols"})
                if (s->entries.count(k)) throw ValueError(file.where(*s, k) + " only applies to feed = databento");
        }
        cfg.venues[mic] = v;
    }
    return cfg;
}

std::map<std::string, std::string> load_keys(const fs::path& path) {
    if (!path.is_absolute()) throw ValueError("--keys must be an absolute path, got `" + path.string() + "`");
    auto file = ini::File::read(path);
    std::map<std::string, std::string> keys;
    for (const auto* s : file.sections()) {
        if (s->name != "databento") throw ValueError(file.where(*s) + " is not a known section (databento)");
        for (const auto& k : s->order) {
            if (!is_mic(k)) throw ValueError(file.where(*s, k) + ": keys are named by MIC, e.g. XNAS = db-...");
            keys[k] = s->entries.at(k).value;
        }
    }
    return keys;
}

void init(const fs::path& config_path, const fs::path& keys_path_) {
    Loaded l;
    l.cfg = load(config_path);
    if (!keys_path_.empty()) {
        l.keys = load_keys(keys_path_);
        l.keys_path = keys_path_;
    }
    current() = std::move(l);
}

void install(Config cfg, std::map<std::string, std::string> keys, fs::path keys_path_) {
    current() = Loaded{std::move(cfg), std::move(keys), std::move(keys_path_)};
}

bool loaded() { return current().has_value(); }

const Config& get() {
    if (!current()) throw RuntimeError("no configuration loaded: pass --config=/full/path/config.ini");
    return current()->cfg;
}

const fs::path& keys_path() {
    static const fs::path none;
    return current() ? current()->keys_path : none;
}

std::string databento_key(const std::string& mic_in) {
    std::string mic = util::upper(mic_in);
    if (!current() || current()->keys_path.empty())
        throw ValueError("no keys file: pass --keys=/full/path/keys.ini to download " + mic);
    auto it = current()->keys.find(mic);
    if (it == current()->keys.end() || it->second.empty())
        throw ValueError("No Databento API key for " + mic + ": set " + mic + " = db-... under [databento] in " +
                         current()->keys_path.string());
    return it->second;
}

const Venue* venue(const std::string& mic) {
    const auto& v = get().venues;
    auto it = v.find(util::upper(mic));
    return it == v.end() ? nullptr : &it->second;
}

std::vector<const Venue*> venues_of_feed(const std::string& feed) {
    std::vector<const Venue*> out;
    for (const auto& [mic, v] : get().venues)
        if (v.feed == feed) out.push_back(&v);
    return out;
}

std::string feed_for(const std::string& mic) {
    const auto* v = venue(mic);
    return v ? v->feed : "";
}

bool owns(const std::string& mic, const std::string& feed) {
    const auto* v = venue(mic);
    return v != nullptr && v->feed == util::lower(util::strip(feed));
}

std::string today() {
    auto zone = date::locate_zone(get().timezone);
    auto local = date::make_zoned(zone, std::chrono::system_clock::now()).get_local_time();
    auto day = date::floor<date::days>(local);
    date::year_month_day ymd{day};
    return util::zero_pad(static_cast<int>(ymd.year()), 4) + util::zero_pad(static_cast<unsigned>(ymd.month()), 2) +
           util::zero_pad(static_cast<unsigned>(ymd.day()), 2);
}

}  // namespace premarket::config
