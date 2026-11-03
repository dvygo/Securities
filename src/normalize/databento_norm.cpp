#include "premarket/normalize/databento_norm.hpp"

#include <databento/dbn_store.hpp>
#include <databento/enums.hpp>

#include <algorithm>
#include <cstring>
#include <set>
#include <unordered_set>

#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/numbering_session.hpp"
#include "premarket/normalize/price.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::databento_norm {

namespace db = databento;

namespace {

template <std::size_t N>
std::string cstr(const std::array<char, N>& a) {
    return std::string(a.data(), strnlen(a.data(), N));
}

std::string ch(char c) { return std::string(1, c); }

template <typename T>
std::string num(T v) {
    if constexpr (std::is_same_v<T, std::int8_t> || std::is_same_v<T, std::uint8_t>) return std::to_string(static_cast<int>(v));
    else return std::to_string(v);
}

std::string ns(db::UnixNanos t) { return std::to_string(t.time_since_epoch().count()); }

const std::map<char, std::pair<const char*, const char*>> kGlbxClassTypes = {
    {'F', {"FUTIDX", "FUTURE"}},  {'C', {"OPTIDX", "OPTION"}},        {'P', {"OPTIDX", "OPTION"}},
    {'S', {"FUTIDX", "FUTURE_SPREAD"}}, {'T', {"OPTIDX", "OPTION_SPREAD"}}, {'M', {"FUTIDX", "MIXED_SPREAD"}},
    {'X', {"FXSPOT", "SPOT"}},    {'Y', {"COMSPOT", "SPOT"}},         {'B', {"BOND", "BOND"}},
    {'K', {"EQUITY", "EQUITY"}},  {'I', {"INDEX", "INDEX"}},
};

int month_number(char c) {
    switch (c) {
        case 'F': return 1; case 'G': return 2; case 'H': return 3; case 'J': return 4;
        case 'K': return 5; case 'M': return 6; case 'N': return 7; case 'Q': return 8;
        case 'U': return 9; case 'V': return 10; case 'X': return 11; case 'Z': return 12;
        default: return 0;
    }
}

std::string class_key(const std::string& instrument_class) { return util::upper(util::strip(instrument_class)); }

void copy_definition_fields(const DefRow& r, row::Row& out) {
    for (std::size_t i = 0; i < r.fields.size(); ++i)
        if (!r.fields[i].empty()) out[row::definition_column(i)] = r.fields[i];
}

std::optional<std::map<std::string, std::string>>& dotted_cache() {
    static std::optional<std::map<std::string, std::string>> cache;
    return cache;
}

}  // namespace

std::vector<std::string> definition_values(const db::InstrumentDefMsg& r) {
    return {
        ns(r.ts_recv), ns(r.hd.ts_event), "instrument-def", num(r.hd.publisher_id),
        ch(static_cast<char>(r.security_update_action)), num(r.raw_instrument_id), num(r.underlying_id),
        num(r.min_price_increment), num(r.display_factor), num(r.high_limit_price), num(r.low_limit_price),
        num(r.max_price_variation), num(r.unit_of_measure_qty), num(r.min_price_increment_amount), num(r.price_ratio),
        num(r.strike_price), ns(r.expiration), ns(r.activation), num(r.inst_attrib_value), num(r.market_depth_implied),
        num(r.market_depth), num(r.market_segment_id), num(r.max_trade_vol), num(r.min_lot_size),
        num(r.min_lot_size_block), num(r.min_lot_size_round_lot), num(r.min_trade_vol), num(r.contract_multiplier),
        num(r.decay_quantity), num(r.original_contract_size), num(r.appl_id), num(r.maturity_year),
        num(r.maturity_month), num(r.maturity_day), num(r.maturity_week), num(r.decay_start_date), num(r.channel_id),
        cstr(r.currency), cstr(r.settl_currency), cstr(r.secsubtype), cstr(r.group), cstr(r.exchange), cstr(r.asset),
        cstr(r.cfi), cstr(r.security_type), cstr(r.unit_of_measure), cstr(r.underlying), cstr(r.strike_price_currency),
        ch(static_cast<char>(r.match_algorithm)), ch(static_cast<char>(r.user_defined_instrument)), num(r.main_fraction),
        num(r.price_display_format), num(r.sub_fraction), num(r.underlying_product), num(r.contract_multiplier_unit),
        num(r.flow_schedule_type), num(r.tick_rule), num(r.leg_count), num(r.leg_index), num(r.leg_instrument_id),
        cstr(r.leg_raw_symbol), ch(static_cast<char>(r.leg_instrument_class)), ch(static_cast<char>(r.leg_side)),
        num(r.leg_price), num(r.leg_delta), num(r.leg_ratio_price_numerator), num(r.leg_ratio_price_denominator),
        num(r.leg_ratio_qty_numerator), num(r.leg_ratio_qty_denominator), num(r.leg_underlying_id),
    };
}

DefRow def_row(const db::InstrumentDefMsg& rec) {
    DefRow r;
    r.instrument_id = rec.hd.instrument_id;
    r.raw_symbol = cstr(rec.raw_symbol);
    r.instrument_class = ch(static_cast<char>(rec.instrument_class));
    r.fields = definition_values(rec);
    return r;
}

std::string script_of(const DefRow& r) { return r.raw_symbol.empty() ? "raw_symbol" : r.raw_symbol; }

std::optional<broker_script::Occ> parse_occ_symbol(const std::string& symbol) {
    std::string s = util::strip(symbol);
    std::size_t end = s.size();
    while (end > 0 && (s[end - 1] == ' ' || s[end - 1] == '\t' || s[end - 1] == '\n' || s[end - 1] == '\r' ||
                       s[end - 1] == '\f' || s[end - 1] == '\v'))
        --end;
    if (end < 15) return std::nullopt;
    std::size_t start = end - 15;
    auto digits = [&](std::size_t from, std::size_t n) {
        for (std::size_t i = from; i < from + n; ++i)
            if (s[i] < '0' || s[i] > '9') return false;
        return true;
    };
    if (!digits(start, 6) || (s[start + 6] != 'C' && s[start + 6] != 'P') || !digits(start + 7, 8)) return std::nullopt;
    broker_script::Occ occ;
    occ.underlying = util::upper(util::replace_all(util::strip(s.substr(0, start)), " ", ""));
    int year = 2000 + std::stoi(s.substr(start, 2));
    int month = std::stoi(s.substr(start + 2, 2));
    int day = std::stoi(s.substr(start + 4, 2));
    occ.expiration = util::zero_pad(year, 4) + util::zero_pad(month, 2) + util::zero_pad(day, 2);
    occ.option_type = s[start + 6] == 'C' ? "CALL" : "PUT";
    occ.strike = static_cast<double>(std::stoll(s.substr(start + 7, 8))) / 1000.0;
    return occ;
}

std::int64_t glbx_expiration_ns(const std::string& symbol_base, const session::Ymd& ref, std::int64_t anchor_ns) {
    if (symbol_base.size() < 2) return 0;
    char month_c = symbol_base[symbol_base.size() - 2];
    char year_c = symbol_base.back();
    int month = month_number(month_c);
    if (month == 0 || year_c < '0' || year_c > '9') return 0;
    int digit = year_c - '0';
    int year_full;
    if (anchor_ns > 0) {
        int anchor_year = util::utc_year_of_ns(anchor_ns);
        int candidate = anchor_year - (anchor_year % 10) + digit;
        year_full = candidate - 10;
        for (int y : {candidate, candidate + 10})
            if (std::abs(y - anchor_year) < std::abs(year_full - anchor_year)) year_full = y;
    } else {
        int anchor_year = ref.year;
        year_full = anchor_year - (anchor_year % 10) + digit;
        if (year_full < anchor_year - 1) year_full += 10;
    }
    return util::utc_epoch_seconds(year_full, month, 1) * 1000000000LL;
}

std::string underlying_root_from_stype_in(const std::string& symbol) {
    if (util::starts_with(symbol, ".")) return symbol.substr(1);
    auto upper_run = [&](std::size_t i) {
        std::size_t j = i;
        while (j < symbol.size() && symbol[j] >= 'A' && symbol[j] <= 'Z') ++j;
        return j;
    };
    std::size_t end = upper_run(0);
    if (end == 0) return symbol;
    while (end < symbol.size() && symbol[end] == '.') {
        std::size_t next = upper_run(end + 1);
        if (next == end + 1) break;
        end = next;
    }
    return symbol.substr(0, end);
}

std::string dotted_underlying(const std::string& root) {
    if (root.empty()) return root;
    auto& cache = dotted_cache();
    if (!cache) {
        std::map<std::string, std::string> out;
        std::set<std::string> clashes;
        for (const char* venue : {"XCBO", "XNAS"}) {
            auto path = paths::baskets_dir() / (std::string(venue) + ".csv");
            std::error_code ec;
            if (!std::filesystem::exists(path, ec)) continue;
            for (const auto& line : util::splitlines(util::read_file(path))) {
                std::string ticker = util::upper(util::strip(line));
                if (ticker.empty() || ticker.find('.') == std::string::npos) continue;
                std::string stripped = util::replace_all(ticker, ".", "");
                auto it = out.find(stripped);
                if (it != out.end() && it->second != ticker) clashes.insert(stripped);
                out[stripped] = ticker;
            }
        }
        for (const auto& c : clashes) out.erase(c);
        cache = std::move(out);
    }
    auto it = cache->find(util::upper(root));
    return it == cache->end() ? root : it->second;
}

void reset_dotted_cache() { dotted_cache().reset(); }

row::Row map_xcme_row(const DefRow& d, const session::Ymd& ref) {
    std::string symbol = script_of(d);
    auto r = row::blank();
    r[row::script] = symbol;
    r[row::scriptToken] = std::to_string(d.instrument_id);
    r[row::scriptDetails] = symbol;
    r[row::currency] = "USD";
    r[row::multiplier] = std::to_string(price::US_PRICE_SCALE);
    r[row::lotSize] = "1";
    r[row::tradingSessionUTC] = session::trading_session_for_xcme(ref);
    std::string klass = class_key(d.instrument_class);
    std::string base = symbol;
    auto opt = broker_script::match_glbx_option(symbol);
    if (opt) {
        base = util::strip(symbol.substr(0, opt->start));
        r[row::optionType] = opt->cp == 'C' ? "CALL" : "PUT";
        r[row::strike] = std::to_string(price::scale_price(*util::parse_float(opt->strike), price::US_PRICE_SCALE));
    } else {
        r[row::strike] = "0";
    }
    auto known = klass.size() == 1 ? kGlbxClassTypes.find(klass[0]) : kGlbxClassTypes.end();
    if (known != kGlbxClassTypes.end()) {
        r[row::scriptInstrumentType] = known->second.first;
        r[row::scriptInstrumentType2] = known->second.second;
        if ((klass == "C" || klass == "P") && r[row::optionType].empty()) r[row::optionType] = klass == "C" ? "CALL" : "PUT";
    } else if (opt) {
        r[row::scriptInstrumentType] = "OPTIDX";
        r[row::scriptInstrumentType2] = "OPTION";
    } else {
        r[row::scriptInstrumentType] = "FUTIDX";
        r[row::scriptInstrumentType2] = "FUTURE";
    }
    std::int64_t venue_expiration = session::as_ns(d.fields[16]);
    std::int64_t contract = glbx_expiration_ns(base, ref, venue_expiration);
    r[row::expiration] = std::to_string(venue_expiration ? venue_expiration : contract);
    r[row::underlying_root] = base;
    r[row::underlying] = base;
    r[row::brokerScript1] = broker_script::from_glbx(symbol, contract);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return r;
}

row::Row map_xcbo_row(const DefRow& d, const session::Ymd& ref) {
    std::string symbol = script_of(d);
    std::string klass = class_key(d.instrument_class);
    auto r = row::blank();
    r[row::script] = symbol;
    r[row::scriptToken] = std::to_string(d.instrument_id);
    r[row::scriptDetails] = symbol;
    r[row::currency] = "USD";
    r[row::multiplier] = std::to_string(price::US_PRICE_SCALE);
    r[row::lotSize] = "100";
    r[row::scriptInstrumentType2] = "OPTION";
    auto parsed = parse_occ_symbol(symbol);
    std::string underlying = parsed ? parsed->underlying : "";
    std::string u = util::upper(underlying);
    bool is_index = u == "SPX" || u == "SPXW" || u == "VIX" || u == "RUT";
    if (parsed) {
        r[row::underlying] = dotted_underlying(underlying);
        r[row::underlying_root] = util::upper(dotted_underlying(underlying));
        r[row::strike] = std::to_string(price::scale_price(parsed->strike, price::US_PRICE_SCALE));
        r[row::optionType] = parsed->option_type;
        auto ymd = session::parse_ymd(parsed->expiration);
        r[row::expiration] = std::to_string(session::xcbo_session_close_utc_seconds(ymd, is_index) * 1000000000LL);
    } else {
        r[row::strike] = "0";
        r[row::expiration] = "0";
    }
    if (klass.size() == 1 && klass[0] == XCBO_SPOT_CLASS) {
        r[row::scriptInstrumentType] = "SPOT";
        r[row::scriptInstrumentType2] = "SPOT";
        r[row::underlying] = symbol;
        r[row::underlying_root] = util::upper(symbol);
        r[row::strike] = "0";
        r[row::optionType] = "";
        r[row::expiration] = "0";
        r[row::brokerScript1] = broker_script::from_equity(symbol);
    } else {
        r[row::brokerScript1] = broker_script::from_occ(symbol, parsed);
        r[row::scriptInstrumentType] = is_index ? "OPTIDX" : "OPTSTK";
    }
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    r[row::tradingSessionUTC] =
        is_index ? session::trading_session_for_xcbo_index(ref) : session::trading_session_for_xcbo_equity(ref);
    return r;
}

row::Row map_xnas_row(const DefRow& d, const session::Ymd& ref) {
    std::string symbol = script_of(d);
    std::string stype_in = d.raw_symbol.empty() ? "raw_symbol" : d.raw_symbol;
    auto r = row::blank();
    r[row::script] = symbol;
    r[row::scriptToken] = std::to_string(d.instrument_id);
    r[row::scriptDetails] = symbol;
    r[row::currency] = "USD";
    r[row::scriptInstrumentType] = "EQUITY";
    r[row::scriptInstrumentType2] = "EQUITY";
    r[row::underlying_root] = underlying_root_from_stype_in(stype_in);
    r[row::underlying] = symbol;
    r[row::strike] = "0";
    r[row::multiplier] = std::to_string(price::US_PRICE_SCALE);
    r[row::lotSize] = "1";
    r[row::expiration] = "0";
    r[row::tradingSessionUTC] = session::trading_session_for_xnas(ref);
    r[row::brokerScript1] = broker_script::from_equity(symbol);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return r;
}

std::vector<std::filesystem::path> definition_files(const std::filesystem::path& dir) {
    std::vector<std::filesystem::path> out;
    for (const auto& p : util::list_dir_sorted(dir)) {
        std::string name = p.filename().string();
        if (util::ends_with(name, ".dbn") || util::ends_with(name, ".dbn.zst")) out.push_back(p);
    }
    return out;
}

void for_each_definition(const std::vector<std::filesystem::path>& files, const std::string& dataset,
                         const std::function<void(const db::InstrumentDefMsg&)>& fn, bool announce_skips) {
    std::unordered_set<std::uint32_t> seen;
    for (const auto& path : files) {
        db::DbnStore store{path};
        const auto& meta = store.GetMetadata();
        if (!meta.schema || *meta.schema != db::Schema::Definition) {
            if (announce_skips)
                log::out("      Skipping " + path.filename().string() + ": schema=" +
                         (meta.schema ? std::string(db::ToString(*meta.schema)) : "None") + ", expected definition");
            continue;
        }
        if (meta.dataset != dataset) {
            if (announce_skips)
                log::out("      Skipping " + path.filename().string() + ": dataset=" + meta.dataset + ", expected " +
                         dataset + " -- wrong venue folder?");
            continue;
        }
        while (const db::Record* rec = store.NextRecord()) {
            if (!rec->Holds<db::InstrumentDefMsg>()) continue;
            const auto& def = rec->Get<db::InstrumentDefMsg>();
            if (!seen.insert(def.hd.instrument_id).second) continue;
            fn(def);
        }
    }
}

namespace {

struct Venue {
    const char* name;
    const char* mic;
    row::Row (*mapper)(const DefRow&, const session::Ymd&);
};

const Venue kVenues[] = {{"xcme", "XCME", map_xcme_row}, {"xcbo", "XCBO", map_xcbo_row}, {"xnas", "XNAS", map_xnas_row}};

}  // namespace

void run(runner::Opts& opts) {
    auto& numbering = numbering_session::of(opts);
    if (opts.dry_run && !numbering.plans_preview()) {
        log::out("DRY RUN: Would normalize Databento data");
        return;
    }
    log::out("  Normalizing Databento data...");
    auto normalized_dir = paths::normalized_dir(opts.date_dir);
    auto ref = session::parse_ymd(opts.date_dir);
    std::filesystem::create_directories(normalized_dir);
    auto token_errors = counter_token::validate(config::get().venues);
    std::set<std::string> mine;
    for (const auto& v : kVenues)
        if (const auto* cfg = config::venue(v.mic); cfg && cfg->feed == "databento") mine.insert(v.mic);
    for (const auto& [mic, msgs] : token_errors) {
        if (!mine.count(mic)) continue;
        for (const auto& msg : msgs) log::out("  CRITICAL [" + mic + "] counterToken config: " + msg);
        log::out("  CRITICAL: skipping " + mic + " -- fix [venue." + mic + "] in " + config::get().file.string() +
                 " before normalizing");
        numbering.record(opts.date_dir, mic, "refused", "counterToken config: " + util::join(msgs, "; "));
    }

    for (const auto& v : kVenues) {
        const auto* cfg = config::venue(v.mic);
        if (cfg == nullptr || cfg->feed != "databento") continue;
        if (!runner::venue_selected(opts, v.mic)) continue;
        if (!cfg->enabled) {
            log::out("    Skipping " + std::string(v.mic) + ": enabled = 0");
            continue;
        }
        if (token_errors.count(v.mic)) continue;
        auto dir = paths::venue_dir(opts.date_dir, v.mic);
        std::error_code ec;
        auto files = std::filesystem::is_directory(dir, ec) ? definition_files(dir) : std::vector<std::filesystem::path>{};
        if (files.empty()) {
            numbering.no_input(opts.date_dir, v.mic, "no definition file in " + dir.filename().string() + "/");
            continue;
        }
        std::string source = std::to_string(files.size()) + " manual file(s) in " + dir.filename().string() + "/";
        log::out("    Processing " + std::string(v.name) + " (" + source + ")...");
        auto output = normalized_dir / (std::string(v.mic) + "-DATABENTO-normalized" + parquet::SUFFIX);

        std::string started_at = counter_token::utc_now();
        std::optional<numbering_session::VenueNumbering> venue_numbering;
        std::optional<numbering::Plan> plan;
        if (cfg->venue_id) {
            try {
                venue_numbering.emplace(numbering.venue(opts.date_dir, v.mic, cfg->venue_id));
                std::vector<std::string> scripts;
                for_each_definition(files, cfg->dataset, [&](const db::InstrumentDefMsg& def) {
                    std::string s = std::string(def.raw_symbol.data(), strnlen(def.raw_symbol.data(), def.raw_symbol.size()));
                    if (!s.empty()) scripts.push_back(std::move(s));
                }, false);
                plan = venue_numbering->plan(scripts);
            } catch (const ValueError& exc) {
                log::out("      CRITICAL: skipping " + std::string(v.name) + " -- " + exc.what());
                numbering.record(opts.date_dir, v.mic, "refused", exc.what());
                continue;
            }
            log::out("      counterTokenV2: " + venue_numbering->describe() + "; " + venue_numbering->summary(*plan));
            if (venue_numbering->preview()) {
                venue_numbering->commit(*plan);
                continue;
            }
            venue_numbering->reserve(*plan);
        }

        parquet::RowWriter writer(output, paths::NORMALIZED_COLUMNS);
        std::size_t total = 0;
        std::vector<row::Row> batch;
        auto flush = [&]() {
            if (batch.empty()) return;
            for (std::size_t n = 0; n < batch.size(); ++n) batch[n][row::counterToken] = std::to_string(total + n + 1);
            if (plan)
                for (auto& r : batch) r[row::counterTokenV2] = plan->rendered(r[row::script]);
            writer.write(batch);
            total += batch.size();
            batch.clear();
            log::out("      " + std::to_string(total) + " row(s)...");
        };
        try {
            for_each_definition(files, cfg->dataset, [&](const db::InstrumentDefMsg& def) {
                auto d = def_row(def);
                auto r = v.mapper(d, ref);
                if (r[row::script].empty()) return;
                copy_definition_fields(d, r);
                batch.push_back(std::move(r));
                if (batch.size() >= NORMALIZE_CHUNK_ROWS) flush();
            });
            flush();
        } catch (const std::exception& e) {
            log::out("      Error processing " + source + ": " + e.what());
            writer.abort();
            continue;
        }
        if (writer.close()) {
            if (plan) {
                std::vector<counter_token::Artifact> inputs;
                for (const auto& f : files) inputs.push_back(counter_token::artifact(f, opts.date_dir));
                venue_numbering->commit(*plan, started_at, inputs,
                                        {counter_token::artifact(output, opts.date_dir, static_cast<std::int64_t>(total))});
            }
            log::out("      Wrote " + std::to_string(total) + " rows to " + output.filename().string());
        } else {
            log::out("      No rows for " + std::string(v.name));
        }
    }
}

}  // namespace premarket::databento_norm
