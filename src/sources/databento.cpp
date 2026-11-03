#include "premarket/sources/databento.hpp"

#include <databento/constants.hpp>
#include <databento/dbn_encoder.hpp>
#include <databento/enums.hpp>
#include <databento/iwritable.hpp>
#include <databento/live.hpp>
#include <databento/live_blocking.hpp>
#include <zstd.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <map>

#include "premarket/log.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::databento_src {

namespace db = databento;

namespace {

struct Buffer : db::IWritable {
    std::string data;
    void WriteAll(const std::byte* buffer, std::size_t length) override {
        data.append(reinterpret_cast<const char*>(buffer), length);
    }
};

std::uint64_t u64(db::UnixNanos t) { return static_cast<std::uint64_t>(t.time_since_epoch().count()); }

std::string utc_hms(std::int64_t ns) { return util::utc_format_ns(ns, "%Y-%m-%d %H:%M:%S"); }

}  // namespace

std::string dataset_key(const std::string& dataset) {
    auto dot = dataset.find('.');
    return util::upper(util::strip(dot == std::string::npos ? dataset : dataset.substr(0, dot)));
}

std::string list_repr(const std::vector<std::string>& items) {
    std::vector<std::string> q;
    for (const auto& s : items) q.push_back(util::quoted(s));
    return "[" + util::join(q, ", ") + "]";
}

std::vector<std::string> resolve_symbols(const config::Venue& venue, const std::optional<std::string>& symbols_file) {
    std::filesystem::path file = symbols_file ? std::filesystem::path(*symbols_file) : venue.symbols;
    if (file.empty()) return {ALL_SYMBOLS};
    std::error_code ec;
    if (!std::filesystem::exists(file, ec)) throw FileNotFoundError("Symbols file not found: " + file.string());
    std::vector<std::string> symbols;
    for (const auto& line : util::split(util::read_file(file), "\n")) {
        std::string s = util::strip(line);
        if (!s.empty()) symbols.push_back(s);
    }
    if (venue.stype_in == "parent")
        for (auto& s : symbols)
            if (!util::starts_with(s, ".") && !util::ends_with(s, ".OPT") && !util::ends_with(s, ".FUT") &&
                !util::ends_with(s, ".SPOT"))
                s += ".OPT";
    return symbols;
}

Selection select_live_definitions(const std::vector<db::InstrumentDefMsg>& records, std::int64_t day_start_ns) {
    std::map<std::uint32_t, const db::InstrumentDefMsg*> latest;
    for (const auto& r : records) latest[r.hd.instrument_id] = &r;
    Selection s;
    s.distinct = latest.size();
    for (const auto& [id, r] : latest) {
        std::uint64_t exp = u64(r->expiration);
        if (exp == UNDEF_TIMESTAMP || exp >= static_cast<std::uint64_t>(day_start_ns)) s.kept.push_back(*r);
    }
    return s;
}

std::string encode_definition_file(const std::vector<db::InstrumentDefMsg>& records, const std::string& dataset,
                                   const std::string& stype_in, const std::vector<std::string>& symbols,
                                   std::int64_t start_ns, std::int64_t end_ns) {
    db::Metadata meta{};
    meta.version = db::kDbnVersion;
    meta.dataset = dataset;
    meta.schema = db::Schema::Definition;
    meta.start = db::UnixNanos{std::chrono::nanoseconds{start_ns}};
    meta.end = db::UnixNanos{std::chrono::nanoseconds{end_ns}};
    meta.limit = 0;
    meta.stype_in = db::FromString<db::SType>(stype_in);
    meta.stype_out = db::SType::InstrumentId;
    meta.ts_out = false;
    meta.symbol_cstr_len = db::kSymbolCstrLen;
    meta.symbols = symbols;
    Buffer raw;
    db::DbnEncoder::EncodeMetadata(meta, &raw);
    for (const auto& r : records) {
        db::Record rec{const_cast<db::RecordHeader*>(&r.hd)};
        db::DbnEncoder::EncodeRecord(rec, &raw);
    }
    std::string out(ZSTD_compressBound(raw.data.size()), '\0');
    auto n = ZSTD_compress(out.data(), out.size(), raw.data.data(), raw.data.size(), 3);
    if (ZSTD_isError(n)) throw RuntimeError(std::string("zstd: ") + ZSTD_getErrorName(n));
    out.resize(n);
    return out;
}

namespace {

std::size_t download_live(const std::string& api_key, const config::Venue& venue, const std::string& stype_in,
                          const std::string& date_dir, const std::filesystem::path& dest_dir,
                          const std::vector<std::string>& symbols) {
    auto now_ns = util::unix_now_ns();
    std::string today = util::utc_format_ns(now_ns, "%Y%m%d");
    if (date_dir != today)
        throw ValueError("live definitions describe the current session only: --date-dir " + date_dir +
                         " is not today (" + today + " UTC)");
    std::string key = dataset_key(venue.dataset);
    if (key == "OPRA") {
        std::string hm = util::utc_format_ns(now_ns, "%H:%M");
        if (hm < "12:00")
            log::out("  WARNING: " + venue.dataset + " lists new contracts at ~12:00Z and it is " + hm +
                     "Z -- today's new listings will be missing from this file");
    }
    log::out("  Live definition replay: " + venue.dataset + " stype_in=" + stype_in + " symbols=" + list_repr(symbols));

    auto client = db::LiveBuilder{}
                      .SetKey(api_key)
                      .SetDataset(venue.dataset)
                      .SetUpgradePolicy(db::VersionUpgradePolicy::UpgradeToV3)
                      .BuildBlocking();
    client.Subscribe(symbols, db::Schema::Definition, db::FromString<db::SType>(stype_in), db::UnixNanos{});
    client.Start();
    std::vector<db::InstrumentDefMsg> definitions;
    bool finished = false;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(LIVE_DEFINITION_REPLAY_CEILING_SEC);
    std::string error;
    while (std::chrono::steady_clock::now() < deadline) {
        const db::Record* rec = client.NextRecord(std::chrono::seconds(30));
        if (rec == nullptr) continue;
        if (rec->Holds<db::InstrumentDefMsg>()) {
            definitions.push_back(rec->Get<db::InstrumentDefMsg>());
        } else if (rec->Holds<db::ErrorMsg>()) {
            error = rec->Get<db::ErrorMsg>().Err();
            break;
        } else if (rec->Holds<db::SystemMsg>()) {
            if (std::strstr(rec->Get<db::SystemMsg>().Msg(), "Finished definition replay")) {
                finished = true;
                break;
            }
        }
    }
    try {
        client.Stop();
    } catch (const std::exception&) {
    }
    if (!error.empty()) throw RuntimeError(venue.dataset + " live replay error: " + error);
    if (!finished)
        throw RuntimeError(venue.dataset + ": no 'Finished definition replay' within " +
                           std::to_string(LIVE_DEFINITION_REPLAY_CEILING_SEC) + "s (" +
                           util::commas(static_cast<std::int64_t>(definitions.size())) + " definitions so far)");

    std::int64_t day_start = util::day_start_ns(date_dir);
    auto sel = select_live_definitions(definitions, day_start);
    if (sel.kept.empty())
        throw RuntimeError(venue.dataset + ": the live replay holds no instrument for " + date_dir + " (" +
                           util::commas(static_cast<std::int64_t>(sel.distinct)) + " distinct, all expired)");
    std::int64_t refreshed = 0;
    std::uint64_t newest = 0;
    for (const auto& r : sel.kept) {
        if (u64(r.ts_recv) >= static_cast<std::uint64_t>(day_start)) ++refreshed;
        newest = std::max(newest, u64(r.ts_recv));
    }
    if ((key == "OPRA" || key == "EQUS") && refreshed == 0)
        throw RuntimeError(venue.dataset + ": nothing in the live replay was sent on " + date_dir +
                           " -- today's daily re-send has not happened yet (see the runbook for times)");
    log::out("  " + util::commas(static_cast<std::int64_t>(definitions.size())) + " definition record(s), " +
             util::commas(static_cast<std::int64_t>(sel.distinct)) + " instrument(s); kept " +
             util::commas(static_cast<std::int64_t>(sel.kept.size())) + " for " + date_dir + ", dropped " +
             util::commas(static_cast<std::int64_t>(sel.distinct - sel.kept.size())) + " expired; " +
             util::commas(refreshed) + " sent today, newest " + utc_hms(static_cast<std::int64_t>(newest)) + "Z");

    auto blob = encode_definition_file(sel.kept, venue.dataset, stype_in, symbols, day_start, util::unix_now_ns());
    std::filesystem::create_directories(dest_dir);
    auto target = dest_dir / (util::replace_all(util::lower(venue.dataset), ".", "-") + "-" + date_dir + ".definition.dbn.zst");
    auto staging = dest_dir / ("." + target.filename().string() + ".tmp." + std::to_string(util::pid()));
    util::write_file(staging, blob);
    util::replace_file(staging, target);
    log::out("    " + target.string());
    return blob.size();
}

}  // namespace

void download(runner::Opts& opts, const std::string& venue_name) {
    std::string mic = util::upper(venue_name);
    const auto* venue = config::venue(mic);
    if (venue == nullptr || venue->feed != "databento") {
        std::vector<std::string> known;
        for (const auto* v : config::venues_of_feed("databento")) known.push_back(util::lower(v->mic));
        throw ValueError("Unknown venue: " + venue_name + ". Venues come from [venue.<MIC>] sections with feed = "
                         "databento; configured: " + (known.empty() ? "(none)" : util::join(known, ", ")));
    }
    if (!venue->enabled)
        throw ValueError(venue_name + " (" + mic + ") is disabled: set enabled = 1 in [venue." + mic + "] to download it");
    std::string api_key = config::databento_key(mic);
    auto symbols = resolve_symbols(*venue, opts.symbols_file);
    bool all = symbols.size() == 1 && symbols[0] == ALL_SYMBOLS;
    std::string stype_in = all ? "raw_symbol" : venue->stype_in;
    if (opts.dry_run) {
        log::out("DRY RUN: Would download " + venue_name + " via definition schema (live replay) stype_in=" + stype_in +
                 " for symbols: " + list_repr(symbols));
        return;
    }
    auto dest = paths::venue_dir(opts.date_dir, mic);
    std::error_code ec;
    if (std::filesystem::is_directory(dest, ec))
        for (const auto& p : util::list_dir_sorted(dest)) {
            std::string n = p.filename().string();
            if (util::ends_with(n, ".dbn") || util::ends_with(n, ".dbn.zst")) {
                log::out("  " + dest.string() + " already has a definition file -- skipping (remove it first to force a re-fetch)");
                return;
            }
        }
    auto bytes = download_live(api_key, *venue, stype_in, opts.date_dir, dest, symbols);
    log::out("Wrote " + util::commas(static_cast<std::int64_t>(bytes)) + " byte(s) to " + dest.string());
}

}  // namespace premarket::databento_src
