#include "premarket/normalize/mcx_contract.hpp"

#include <date/tz.h>

#include <cmath>
#include <set>

#include "premarket/config.hpp"
#include "premarket/csv.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/broker_script.hpp"
#include "premarket/normalize/fields.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"
#include "premarket/zipread.hpp"

namespace premarket::mcx_contract {

namespace {

const std::set<std::string> kInstrumentTypes = {"FUTCOM", "OPTFUT", "FUTIDX", "OPTIDX", "COM"};
const std::set<std::string> kMetalsEnergy = {"PRECIOUS_METAL",  "NON_FERROUS_METAL", "FERROUS_METAL",
                                             "ENERGY_PRODUCTS", "INDICES",           "PLASTICS"};
const std::set<std::string> kAgri = {"AGRO_PRODUCTS", "FOOD_GRAINS", "JAGGERY",    "OIL",       "OIL_SEEDS", "OIL_SEED_MEALS",
                                     "PLANTATION",    "SPICES",      "VEGETABLE", "FIBRE",     "OTHERS",    "WEATHER"};
const std::set<std::string> kIntlAgri = {"COTTON", "CPO", "KAPAS", "MENTHAOIL"};
const char* kMonths[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

struct Hm {
    int h, m;
};
struct ProfileConf {
    Hm open, close_dst, close_std;
};

const ProfileConf& conf(Profile p) {
    static const ProfileConf metals{{9, 0}, {23, 30}, {23, 55}};
    static const ProfileConf agri{{9, 0}, {17, 0}, {17, 0}};
    static const ProfileConf intl{{9, 0}, {21, 0}, {21, 30}};
    return p == Profile::MetalsEnergy ? metals : p == Profile::Agri ? agri : intl;
}

std::int64_t to_int(const std::string& value, std::int64_t fallback) {
    auto f = util::parse_float(util::strip(value));
    if (!f || std::isnan(*f) || std::isinf(*f)) return fallback;
    return static_cast<std::int64_t>(std::trunc(*f));
}

std::optional<session::Ymd> utc_date(std::int64_t unix) {
    if (unix <= 0) return std::nullopt;
    std::string s = util::utc_format_ns(unix * 1000000000LL, "%Y%m%d");
    return session::Ymd{std::stoi(s.substr(0, 4)), std::stoi(s.substr(4, 2)), std::stoi(s.substr(6, 2))};
}

std::string ymd_text(const session::Ymd& d) {
    return util::zero_pad(d.year, 4) + util::zero_pad(d.month, 2) + util::zero_pad(d.day, 2);
}

std::int64_t ist_to_utc_seconds(const session::Ymd& day, Hm hm) {
    return util::utc_epoch_seconds(day.year, day.month, day.day, hm.h, hm.m) - (5 * 3600 + 30 * 60);
}

Hm close_of(Profile p, const session::Ymd& day) {
    return us_on_standard_time(day) ? conf(p).close_std : conf(p).close_dst;
}

std::string hhmm_utc(std::int64_t seconds) { return util::utc_format_ns(seconds * 1000000000LL, "%H%M"); }

std::string human_date(const std::optional<session::Ymd>& d) {
    if (!d) return "";
    return util::zero_pad(d->day, 2) + " " + kMonths[d->month - 1] + " " + util::zero_pad(d->year % 100, 2);
}

}  // namespace

std::filesystem::path drop_dir(const std::string& as_of) { return paths::venue_dir(as_of, DROP_DIR); }

std::optional<std::filesystem::path> find_zip(const std::string& as_of) {
    auto dir = drop_dir(as_of);
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return std::nullopt;
    std::vector<std::filesystem::path> zips;
    for (const auto& p : util::list_dir_sorted(dir))
        if (std::filesystem::is_regular_file(p, ec) && util::lower(p.extension().string()) == ".zip") zips.push_back(p);
    if (zips.empty()) return std::nullopt;
    if (zips.size() > 1) {
        std::vector<std::string> names;
        for (const auto& z : zips) names.push_back(z.filename().string());
        throw DropError(std::to_string(zips.size()) + " zip files in " + dir.string() + " (" + util::join(names, ", ") +
                        "); leave exactly one");
    }
    return zips.front();
}

std::vector<std::vector<std::string>> read_member(const std::filesystem::path& zip) {
    auto all = zipread::names(zip);
    std::string member;
    for (const auto& n : all)
        if (util::lower(std::filesystem::path(n).filename().string()) == util::lower(MEMBER)) {
            member = n;
            break;
        }
    if (member.empty())
        throw DropError(zip.filename().string() + " has no " + MEMBER + " (it holds: " + util::join(all, ", ") + ")");
    std::string text = util::latin1_to_utf8(zipread::read(zip, member));
    std::vector<std::vector<std::string>> rows;
    for (auto& rec : csv::parse_records(text)) {
        if (rec.empty()) continue;
        for (auto& cell : rec) cell = util::strip(cell);
        rows.push_back(std::move(rec));
    }
    return rows;
}

bool us_on_standard_time(const session::Ymd& day) {
    static const date::time_zone* zone = date::locate_zone("America/New_York");
    date::local_seconds noon = date::local_days{date::year{day.year} / date::month{static_cast<unsigned>(day.month)} /
                                                date::day{static_cast<unsigned>(day.day)}} +
                               std::chrono::hours{12};
    return zone->get_info(noon).first.save == std::chrono::minutes{0};
}

Profile profile_for(const std::string& group, const std::string& symbol) {
    if (kIntlAgri.count(symbol)) return Profile::IntlAgri;
    if (kMetalsEnergy.count(group)) return Profile::MetalsEnergy;
    if (kAgri.count(group)) return Profile::Agri;
    return Profile::None;
}

std::string session_utc(Profile profile, const session::Ymd& day) {
    if (profile == Profile::None) return "";
    return hhmm_utc(ist_to_utc_seconds(day, conf(profile).open)) + "-" + hhmm_utc(ist_to_utc_seconds(day, close_of(profile, day)));
}

std::int64_t expiration_ns(std::int64_t expiry_unix, Profile profile) {
    auto day = utc_date(expiry_unix);
    if (!day) return 0;
    if (profile == Profile::None) return expiry_unix * 1000000000LL;
    return ist_to_utc_seconds(*day, close_of(profile, *day)) * 1000000000LL;
}

std::string strike_text(std::int64_t paise) {
    if (paise <= 0) return "";
    std::string text = util::fixed(static_cast<double>(paise) / static_cast<double>(SCALE), 6);
    text = util::rstrip_chars(util::rstrip_chars(text, "0"), ".");
    return text.empty() ? "0" : text;
}

std::string script_for(const std::vector<std::string>& row) {
    const std::string& symbol = row[COL_SYMBOL];
    const std::string& inst = row[COL_INST];
    if (inst == "COM") return symbol;
    auto day = utc_date(to_int(row[COL_EXPIRY], 0));
    std::string yymon = day ? util::zero_pad(day->year % 100, 2) + util::upper(kMonths[day->month - 1]) : "";
    if (util::starts_with(inst, "OPT")) return symbol + yymon + strike_text(to_int(row[COL_STRIKE], 0)) + row[COL_OPTION];
    return symbol + yymon + "FUT";
}

bool is_spread(const std::vector<std::string>& row) { return row[COL_INST] == "FUTCOM" && row[COL_PRODUCT_CODE] == "0"; }

row::Row map_row(const std::vector<std::string>& row, const session::Ymd& as_of_day) {
    const std::string& symbol = row[COL_SYMBOL];
    const std::string& inst = row[COL_INST];
    std::string option = util::upper(row[COL_OPTION]);
    std::int64_t expiry_unix = inst != "COM" ? to_int(row[COL_EXPIRY], 0) : 0;
    auto day = utc_date(expiry_unix);
    Profile profile = profile_for(row[COL_GROUP], symbol);
    bool is_opt = util::starts_with(inst, "OPT");
    std::int64_t strike = is_opt ? to_int(row[COL_STRIKE], 0) : 0;
    std::string script = script_for(row);
    std::string details;
    if (inst == "COM") {
        details = row[COL_DESC].empty() ? symbol : row[COL_DESC];
    } else {
        std::vector<std::string> parts = {symbol, human_date(day)};
        if (is_opt) {
            parts.push_back(strike_text(strike));
            parts.push_back(option);
        } else {
            parts.push_back("FUT");
        }
        std::vector<std::string> kept;
        for (const auto& p : parts)
            if (!p.empty()) kept.push_back(p);
        details = util::join(kept, " ");
    }
    auto r = row::blank();
    r[row::script] = script;
    r[row::scriptToken] = row[COL_TOKEN];
    r[row::scriptDetails] = details;
    r[row::scriptInstrumentType] = inst;
    r[row::scriptInstrumentType2] = fields::instrument_type2(inst);
    r[row::ISIN] = "";
    r[row::multiplier] = std::to_string(SCALE);
    std::int64_t lot = to_int(row[COL_LOT], 1);
    r[row::lotSize] = std::to_string(lot ? lot : 1);
    r[row::tickSize] = std::to_string(to_int(row[COL_TICK], 0));
    r[row::tradingSessionUTC] = session_utc(profile, as_of_day);
    r[row::expiration] = std::to_string(expiration_ns(expiry_unix, profile));
    r[row::underlying] = symbol;
    r[row::underlying_root] = symbol;
    r[row::strike] = std::to_string(strike > 0 ? strike : 0);
    r[row::optionType] = option == "CE" ? "CALL" : option == "PE" ? "PUT" : "";
    r[row::currency] = row[COL_CURRENCY].empty() ? "INR" : row[COL_CURRENCY];
    r[row::brokerScript1] = broker_script::from_equity(script);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return r;
}

std::pair<std::vector<row::Row>, Stats> rows_for(const std::string& as_of, const std::filesystem::path& zip) {
    auto as_of_day = session::parse_ymd(as_of);
    std::string as_of_text = ymd_text(as_of_day);
    auto raw = read_member(zip);
    Stats stats;
    stats.read = raw.size();
    std::vector<row::Row> rows;
    std::set<std::string> seen;
    bool file_date_seen = false;
    for (const auto& row : raw) {
        if (row.size() != WIDTH) {
            ++stats.malformed;
            continue;
        }
        if (!file_date_seen) {
            if (auto d = utc_date(to_int(row[COL_FILE_TS], 0))) {
                stats.file_date = ymd_text(*d);
                file_date_seen = true;
            }
        }
        const std::string& inst = row[COL_INST];
        if (!kInstrumentTypes.count(inst)) {
            stats.unknown_type[inst] += 1;
            continue;
        }
        if (is_spread(row)) {
            ++stats.spreads;
            continue;
        }
        if (inst != "COM") {
            auto day = utc_date(to_int(row[COL_EXPIRY], 0));
            if (!day || ymd_text(*day) < as_of_text) {
                ++stats.expired;
                continue;
            }
        }
        if (profile_for(row[COL_GROUP], row[COL_SYMBOL]) == Profile::None) stats.unknown_group[row[COL_GROUP]] += 1;
        auto mapped = map_row(row, as_of_day);
        if (!seen.insert(mapped[row::script]).second) {
            ++stats.collisions;
            continue;
        }
        rows.push_back(std::move(mapped));
    }
    return {std::move(rows), stats};
}

namespace {

void report(const Stats& stats, std::size_t kept, const std::string& as_of) {
    log::out("    " + std::string(MEMBER) + ": " + util::commas(static_cast<std::int64_t>(stats.read)) + " row(s) read, " +
             util::commas(static_cast<std::int64_t>(kept)) + " kept");
    if (stats.file_date && *stats.file_date != as_of) {
        int age = util::days_between(*stats.file_date, as_of);
        std::string when = age > 0 ? std::to_string(age) + " day(s) before" : std::to_string(-age) + " day(s) after";
        std::string iso = stats.file_date->substr(0, 4) + "-" + stats.file_date->substr(4, 2) + "-" + stats.file_date->substr(6, 2);
        log::out("    WARNING: " + std::string(MEMBER) + " is dated " + iso + ", " + when + " " + as_of);
    }
    const std::pair<std::size_t, std::string> counted[] = {
        {stats.expired, "expired before the run date"},
        {stats.spreads, "calendar spreads"},
        {stats.malformed, "rows not " + std::to_string(WIDTH) + " columns wide"},
        {stats.collisions, "a script already claimed by an earlier row"},
    };
    for (const auto& [n, label] : counted)
        if (n) log::out("    " + util::commas(static_cast<std::int64_t>(n)) + " dropped: " + label);
    const std::pair<const std::map<std::string, std::int64_t>*, std::string> grouped[] = {
        {&stats.unknown_type, "dropped, instrument type not handled"},
        {&stats.unknown_group, "kept, commodity group not in the session table (blank session, the file's own expiry time)"},
    };
    for (const auto& [found, label] : grouped) {
        if (found->empty()) continue;
        std::int64_t total = 0;
        std::vector<std::string> parts;
        for (const auto& [k, n] : *found) {
            total += n;
            parts.push_back((k.empty() ? std::string("(blank)") : k) + "=" + util::commas(n));
        }
        log::out("    " + util::commas(total) + " " + label + ": " + util::join(parts, ", "));
    }
}

}  // namespace

void run(runner::Opts& opts) {
    if (!config::owns(MIC, FEED)) {
        log::out(std::string("  Skipping MCX contract file: ") + MIC + " feed = " + config::feed_for(MIC));
        return;
    }
    if (!runner::venue_selected(opts, MIC)) return;
    std::optional<std::filesystem::path> zip;
    try {
        zip = find_zip(opts.date_dir);
    } catch (const DropError& exc) {
        log::out(std::string("  CRITICAL: skipping ") + MIC + " -- " + exc.what());
        return;
    }
    if (!zip) {
        log::out(std::string("  No MCX contract zip for ") + MIC + " (" + drop_dir(opts.date_dir).string() + ")");
        return;
    }
    log::out("  Normalizing MCX contract file (" + zip->filename().string() + ")...");
    std::vector<row::Row> rows;
    Stats stats;
    try {
        std::tie(rows, stats) = rows_for(opts.date_dir, *zip);
    } catch (const ValueError& exc) {
        log::out(std::string("  CRITICAL: skipping ") + MIC + " -- " + exc.what());
        return;
    }
    report(stats, rows.size(), opts.date_dir);
    if (rows.empty()) {
        log::out("    No live contracts -- nothing written");
        return;
    }
    for (std::size_t n = 0; n < rows.size(); ++n) {
        rows[n][row::counterToken] = std::to_string(n + 1);
        rows[n][row::counterTokenV2] = rows[n][row::scriptToken];
    }
    if (opts.dry_run) {
        log::out("    DRY RUN: would write " + util::commas(static_cast<std::int64_t>(rows.size())) + " rows to " + OUTPUT);
        return;
    }
    auto output = paths::normalized_dir(opts.date_dir) / OUTPUT;
    std::filesystem::create_directories(output.parent_path());
    parquet::write_rows(output, paths::NORMALIZED_COLUMNS, rows);
    log::out("    Wrote " + util::commas(static_cast<std::int64_t>(rows.size())) + " rows to " + output.filename().string());
}

}  // namespace premarket::mcx_contract
