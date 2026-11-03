#include "premarket/normalize/fields.hpp"

#include <cmath>

#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/broker_script.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/numbering_session.hpp"
#include "premarket/normalize/price.hpp"
#include "premarket/normalize/session.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::fields {

namespace {

const std::string& get(const fyers::Row& r, const std::string& key) {
    static const std::string empty;
    auto it = r.find(key);
    return it == r.end() ? empty : it->second;
}

std::string to_string_i128(__int128 v) {
    if (v == 0) return "0";
    bool neg = v < 0;
    unsigned __int128 u = neg ? static_cast<unsigned __int128>(-v) : static_cast<unsigned __int128>(v);
    std::string s;
    while (u) {
        s.push_back(static_cast<char>('0' + static_cast<int>(u % 10)));
        u /= 10;
    }
    if (neg) s.push_back('-');
    return {s.rbegin(), s.rend()};
}

}  // namespace

std::string instrument_type2(const std::string& inst_type) {
    std::string t = util::upper(inst_type);
    if (t == "EQ") return "EQUITY";
    if (util::starts_with(t, "FUT")) return "FUTURE";
    if (util::starts_with(t, "OPT")) return "OPTION";
    return t;
}

std::optional<std::int64_t> safe_int(const std::string& raw) {
    auto f = util::parse_float(raw);
    if (!f) return std::nullopt;
    if (std::isnan(*f)) return std::nullopt;
    if (std::isinf(*f)) throw ValueError("cannot convert float infinity to integer");
    return static_cast<std::int64_t>(std::trunc(*f));
}

std::string classify_instrument(const std::string& ex_inst_type) {
    std::string code = util::strip(ex_inst_type);
    if (auto n = safe_int(code)) {
        auto it = fyers::INSTRUMENT_CODES.find(static_cast<int>(*n));
        if (it != fyers::INSTRUMENT_CODES.end() && !it->second.empty()) return it->second;
    }
    return code.empty() ? "UNKNOWN" : "UNKNOWN_" + code;
}

std::string expiration_ns(const std::string& raw) {
    std::string s = util::strip(raw);
    if (s.empty() || s == "0" || s == "-1") return "0";
    auto f = util::parse_float(s);
    if (!f) {
        if (util::valid_yyyymmdd(s)) return std::to_string(util::day_start_ns(s));
        return "0";
    }
    if (std::isnan(*f) || std::isinf(*f)) throw ValueError("cannot convert float to integer: " + s);
    __int128 ts = static_cast<__int128>(std::trunc(*f));
    if (ts <= 0) return "0";
    if (ts < 1000000000000LL) return to_string_i128(ts * 1000000000);
    if (ts < 1000000000000000LL) return to_string_i128(ts * 1000000);
    return to_string_i128(ts);
}

Mapped map_fyers_row(const fyers::Row& raw) {
    Mapped m;
    auto& r = m.row;
    r = row::blank();
    r[row::script] = get(raw, "symTicker");
    r[row::scriptToken] = get(raw, "exToken");
    r[row::scriptDetails] = get(raw, "symDetails").empty() ? r[row::script] : get(raw, "symDetails");
    const std::string& exchange = get(raw, "exchange");
    const std::string& segment = get(raw, "segment");
    m.exchange = fyers::resolve_exchange_mic(exchange, segment);
    std::string inst = classify_instrument(get(raw, "exInstType"));
    r[row::scriptInstrumentType] = inst;
    r[row::scriptInstrumentType2] = instrument_type2(inst);
    std::string opt = util::upper(util::strip(get(raw, "optType")));
    r[row::optionType] = opt == "CE" ? "CALL" : opt == "PE" ? "PUT" : "";
    r[row::ISIN] = get(raw, "isin");
    r[row::multiplier] = std::to_string(price::INDIA_PRICE_SCALE);
    auto tick_it = raw.find("tickSize");
    r[row::tickSize] = std::to_string(tick_it == raw.end() ? 0 : price::scale_price_text(tick_it->second));
    const std::string& strike = get(raw, "strikePrice");
    bool strike_valid = false;
    if (!strike.empty())
        if (auto f = util::parse_float(strike)) strike_valid = *f > 0;
    r[row::strike] = std::to_string(strike_valid ? price::scale_price_text(strike) : 0);
    auto lot_it = raw.find("minLotSize");
    std::int64_t lot = 1;
    if (lot_it != raw.end() && !lot_it->second.empty()) {
        auto f = util::parse_float(lot_it->second);
        if (f && std::isinf(*f)) throw ValueError("cannot convert float infinity to integer");
        lot = f && !std::isnan(*f) ? static_cast<std::int64_t>(std::trunc(*f)) : 1;
    }
    r[row::lotSize] = std::to_string(lot);
    r[row::currency] = "INR";
    r[row::underlying] = get(raw, "exSymName");
    r[row::underlying_root] = get(raw, "exSymName");
    std::string session_utc = session::trading_session_ist_to_utc(get(raw, "tradingSession"));
    auto seg = safe_int(segment);
    bool is_cm = seg && fyers::SEGMENT_CODES.count(static_cast<int>(*seg)) &&
                 fyers::SEGMENT_CODES.at(static_cast<int>(*seg)) == "CM";
    if (is_cm && !session_utc.empty()) session_utc = std::string(session::NSE_PREOPEN_UTC) + "|" + session_utc;
    r[row::tradingSessionUTC] = session_utc;
    r[row::expiration] = expiration_ns(get(raw, "expiryDate"));
    r[row::brokerScript1] = broker_script::from_equity(r[row::script]);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return m;
}

void run(runner::Opts& opts) {
    auto& numbering = numbering_session::of(opts);
    if (opts.dry_run && !numbering.plans_preview()) {
        log::out("DRY RUN: Would normalize Fyers data");
        return;
    }
    auto normalized_dir = paths::normalized_dir(opts.date_dir);
    std::filesystem::create_directories(normalized_dir);
    auto token_errors = counter_token::validate(config::get().venues);

    for (const auto& bundle : paths::FYERS_MIC_BUNDLES) {
        const std::string& mic = bundle.mic;
        if (!runner::venue_selected(opts, mic)) continue;
        const auto* mic_cfg = counter_token::exchange_for(mic);
        if (mic_cfg != nullptr && !mic_cfg->enabled) {
            log::out("  Skipping Fyers " + mic + ": enabled = 0");
            continue;
        }
        if (!config::owns(mic, "fyers")) {
            log::out("  Skipping Fyers " + mic + ": feed = " + config::feed_for(mic));
            continue;
        }
        if (auto it = token_errors.find(mic); it != token_errors.end()) {
            for (const auto& msg : it->second) log::out("  CRITICAL [" + mic + "] counterToken config: " + msg);
            log::out("  CRITICAL: skipping Fyers " + mic + " -- fix [venue." + mic + "] in " + config::get().file.string());
            numbering.record(opts.date_dir, mic, "refused", "counterToken config: " + util::join(it->second, "; "));
            continue;
        }
        auto bundle_dir = paths::venue_dir(opts.date_dir, mic);
        std::error_code ec;
        if (!std::filesystem::is_directory(bundle_dir, ec)) {
            log::out("  No raw directory for Fyers " + mic + " (" + bundle_dir.string() + ")");
            numbering.no_input(opts.date_dir, mic, "no raw directory " + bundle_dir.filename().string() + "/");
            continue;
        }
        log::out("  Normalizing Fyers " + mic + "...");

        std::vector<row::Row> all_rows;
        std::vector<std::filesystem::path> used_sources;
        for (const auto& source_file : bundle.sources) {
            auto source_path = bundle_dir / source_file;
            if (!std::filesystem::exists(source_path, ec)) continue;
            used_sources.push_back(source_path);
            for (const auto& raw : fyers::parse_fyers_csv(source_path)) {
                auto mapped = map_fyers_row(raw);
                if (!mapped.row[row::script].empty() && !mapped.exchange.empty()) all_rows.push_back(std::move(mapped.row));
            }
        }

        std::string started_at = counter_token::utc_now();
        std::optional<numbering_session::VenueNumbering> venue_numbering;
        std::optional<numbering::Plan> venue_plan;
        const auto* exchange_cfg = counter_token::exchange_for(mic);
        if (exchange_cfg != nullptr && exchange_cfg->venue_id) {
            for (std::size_t n = 0; n < all_rows.size(); ++n) all_rows[n][row::counterToken] = std::to_string(n + 1);
            try {
                venue_numbering.emplace(numbering.venue(opts.date_dir, mic, exchange_cfg->venue_id));
                std::vector<std::string> scripts;
                scripts.reserve(all_rows.size());
                for (const auto& r : all_rows) scripts.push_back(r[row::script]);
                venue_plan = venue_numbering->plan(scripts);
            } catch (const ValueError& exc) {
                log::out("  CRITICAL: skipping Fyers " + mic + " -- " + exc.what());
                numbering.record(opts.date_dir, mic, "refused", exc.what());
                continue;
            }
            for (auto& r : all_rows) r[row::counterTokenV2] = venue_plan->rendered(r[row::script]);
            log::out("    " + mic + " counterTokenV2: " + venue_numbering->describe() + "; " +
                     venue_numbering->summary(*venue_plan));
            if (venue_numbering->preview()) {
                venue_numbering->commit(*venue_plan);
                continue;
            }
        }

        auto output_path = normalized_dir / bundle.output;
        if (!all_rows.empty() && venue_plan) venue_numbering->reserve(*venue_plan);
        if (!all_rows.empty()) {
            parquet::RowWriter writer(output_path, paths::NORMALIZED_COLUMNS);
            for (std::size_t i = 0; i < all_rows.size(); i += parquet::CHUNK_ROWS) {
                std::vector<row::Row> chunk(all_rows.begin() + static_cast<std::ptrdiff_t>(i),
                                            all_rows.begin() + static_cast<std::ptrdiff_t>(std::min(all_rows.size(), i + parquet::CHUNK_ROWS)));
                writer.write(chunk);
            }
            writer.close();
            if (venue_plan) {
                std::vector<counter_token::Artifact> inputs;
                for (const auto& src : used_sources) inputs.push_back(counter_token::artifact(src, opts.date_dir));
                venue_numbering->commit(*venue_plan, started_at, inputs,
                                        {counter_token::artifact(output_path, opts.date_dir,
                                                                 static_cast<std::int64_t>(all_rows.size()))});
            }
            log::out("    Wrote " + std::to_string(all_rows.size()) + " rows to " + output_path.string());
        } else {
            log::out("    No rows for " + mic);
        }
    }
}

}  // namespace premarket::fields
