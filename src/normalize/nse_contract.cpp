#include "premarket/normalize/nse_contract.hpp"

#include <algorithm>
#include <cmath>
#include <set>

#include "premarket/config.hpp"
#include "premarket/csv.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/broker_script.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/normalize/fields.hpp"
#include "premarket/normalize/numbering_session.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::nse_contract {

namespace {

struct SegmentConf {
    std::int64_t scale;
    const char* session;
    int close_h;
    int close_m;
    bool has_close;
};

const SegmentConf& conf(Segment s) {
    static const SegmentConf cash{100, "0330-0338|0345-1000|1245-1345", 0, 0, false};
    static const SegmentConf deriv{100, "0345-1010|1245-1345", 10, 10, true};
    static const SegmentConf currency{10000000, "0330-1130|1245-1345", 7, 0, true};
    return s == Segment::Cash ? cash : s == Segment::Deriv ? deriv : currency;
}

const std::set<std::string> kSeriesEquity = {"EQ", "SM", "BE", "ST", "BZ", "RR", "E1", "IT", "SZ"};
const std::map<std::string, std::string> kSeriesTypes = {{"SG", "SGB"},      {"GS", "G-SECS"}, {"MF", "MF"},
                                                         {"TB", "T-BILLS"},  {"W1", "WARRANTS"},
                                                         {"SF", "MISC"},     {"D1", "MISC"}};
const char* kSeriesDefault = "DEBENTURES";
const std::map<std::string, std::map<std::string, std::string>> kSeriesByFlag = {
    {"EQ", {{"0", "EQ"}, {"4", "ETF"}}},
    {"IV", {{"0", "EQ"}, {"4", "MISC"}}},
};
const char* kMonths[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

std::string field(const Raw& r, const std::string& key) {
    auto it = r.find(key);
    return it == r.end() ? std::string() : util::strip(it->second);
}

std::int64_t to_int(const std::string& value, std::int64_t fallback) {
    auto f = util::parse_float(util::strip(value));
    if (!f || std::isnan(*f) || std::isinf(*f)) return fallback;
    return static_cast<std::int64_t>(std::trunc(*f));
}

std::string human_date(std::int64_t unix) {
    if (unix <= 0) return "";
    int y = std::stoi(util::utc_format_ns(unix * 1000000000LL, "%Y"));
    int m = std::stoi(util::utc_format_ns(unix * 1000000000LL, "%m"));
    std::string d = util::utc_format_ns(unix * 1000000000LL, "%d");
    return d + " " + kMonths[m - 1] + " " + util::zero_pad(y % 100, 2);
}

std::string strike_text(const std::string& raw, std::int64_t scale) {
    std::int64_t value = to_int(raw, 0);
    if (value <= 0) return "";
    std::string text = util::fixed(static_cast<double>(value) / static_cast<double>(scale), 6);
    text = util::rstrip_chars(util::rstrip_chars(text, "0"), ".");
    return text.empty() ? "0" : text;
}

}  // namespace

std::filesystem::path drop_dir(const std::string& as_of) { return paths::venue_dir(as_of, "XNSE") / DROP_DIR; }

bool present(const std::string& as_of) {
    auto dir = drop_dir(as_of);
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return false;
    for (const char* name : {CM_FILE, FO_FILE, CD_FILE})
        if (!std::filesystem::exists(dir / name, ec)) return false;
    return true;
}

std::int64_t to_unix_seconds(const std::string& nse_seconds) {
    std::int64_t raw = to_int(nse_seconds, 0);
    return raw <= 0 ? 0 : raw + NSE_EPOCH_OFFSET;
}

std::int64_t expiration_ns(const std::string& nse_seconds, Segment segment) {
    std::int64_t unix = to_unix_seconds(nse_seconds);
    const auto& c = conf(segment);
    if (unix <= 0 || !c.has_close) return 0;
    std::string ymd = util::utc_format_ns(unix * 1000000000LL, "%Y%m%d");
    return util::utc_epoch_seconds(std::stoi(ymd.substr(0, 4)), std::stoi(ymd.substr(4, 2)), std::stoi(ymd.substr(6, 2)),
                                   c.close_h, c.close_m) *
           1000000000LL;
}

std::string cash_instrument_type(const Raw& row, const std::map<std::string, std::string>* by_isin) {
    std::string series = util::upper(field(row, "SctySrs"));
    if (auto it = kSeriesByFlag.find(series); it != kSeriesByFlag.end()) {
        std::string flag = field(row, "SctyTpFlg");
        auto t = it->second.find(flag);
        return t != it->second.end() ? t->second : it->second.at("0");
    }
    if (auto it = kSeriesTypes.find(series); it != kSeriesTypes.end()) return it->second;
    if (kSeriesEquity.count(series)) return "EQ";
    if (by_isin && !by_isin->empty()) {
        auto it = by_isin->find(field(row, "ISIN"));
        if (it != by_isin->end() && !it->second.empty()) return it->second;
    }
    return kSeriesDefault;
}

std::map<std::string, std::string> types_by_isin(const std::vector<Raw>& rows) {
    std::map<std::string, std::string> index;
    for (const auto& row : rows) {
        std::string series = util::upper(field(row, "SctySrs"));
        std::string isin = field(row, "ISIN");
        if (isin.empty() || index.count(isin)) continue;
        if (auto it = kSeriesByFlag.find(series); it != kSeriesByFlag.end()) {
            std::string flag = field(row, "SctyTpFlg");
            auto t = it->second.find(flag);
            index[isin] = t != it->second.end() ? t->second : it->second.at("0");
        } else if (auto st = kSeriesTypes.find(series); st != kSeriesTypes.end()) {
            index[isin] = st->second;
        } else if (kSeriesEquity.count(series)) {
            index[isin] = "EQ";
        }
    }
    return index;
}

std::optional<row::Row> map_cash_row(const Raw& raw, const std::map<std::string, std::string>* by_isin) {
    std::string ticker = field(raw, "TckrSymb");
    std::string series = util::upper(field(raw, "SctySrs"));
    if (ticker.empty() || series.empty()) return std::nullopt;
    std::string script = "NSE:" + ticker + "-" + series;
    std::string inst = cash_instrument_type(raw, by_isin);
    auto r = row::blank();
    r[row::script] = script;
    r[row::scriptToken] = field(raw, "FinInstrmId");
    std::string name = field(raw, "FinInstrmNm");
    r[row::scriptDetails] = name.empty() ? ticker : name;
    r[row::scriptInstrumentType] = inst;
    r[row::scriptInstrumentType2] = fields::instrument_type2(inst);
    r[row::ISIN] = field(raw, "ISIN");
    r[row::multiplier] = std::to_string(conf(Segment::Cash).scale);
    std::int64_t lot = to_int(raw.count("NewBrdLotQty") ? raw.at("NewBrdLotQty") : "None", 1);
    r[row::lotSize] = std::to_string(lot ? lot : 1);
    r[row::tickSize] = std::to_string(to_int(raw.count("BidIntrvl") ? raw.at("BidIntrvl") : "None", 0));
    r[row::tradingSessionUTC] = conf(Segment::Cash).session;
    r[row::expiration] = "0";
    r[row::underlying] = ticker;
    r[row::underlying_root] = ticker;
    r[row::strike] = "0";
    r[row::currency] = "INR";
    r[row::brokerScript1] = broker_script::from_equity(script);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return r;
}

std::optional<row::Row> map_derivative_row(const Raw& raw, Segment segment) {
    std::string name = field(raw, "StockNm");
    std::string ticker = field(raw, "TckrSymb");
    if (name.empty() || ticker.empty()) return std::nullopt;
    const auto& c = conf(segment);
    std::string script = "NSE:" + name;
    std::string inst = util::upper(field(raw, "FinInstrmNm"));
    std::string option = util::upper(field(raw, "OptnTp"));
    std::string xpry = raw.count("XpryDt") ? raw.at("XpryDt") : "None";
    std::string strk = raw.count("StrkPric") ? raw.at("StrkPric") : "None";
    std::int64_t unix = to_unix_seconds(xpry);
    std::int64_t strike_raw = to_int(strk, 0);
    std::vector<std::string> parts = {ticker, human_date(unix)};
    if (option == "CE" || option == "PE") {
        parts.push_back(strike_text(strk, c.scale));
        parts.push_back(option);
    } else {
        parts.push_back("FUT");
    }
    std::vector<std::string> kept;
    for (const auto& p : parts)
        if (!p.empty()) kept.push_back(p);
    auto r = row::blank();
    r[row::script] = script;
    r[row::scriptToken] = field(raw, "FinInstrmId");
    r[row::scriptDetails] = util::join(kept, " ");
    r[row::scriptInstrumentType] = inst;
    r[row::scriptInstrumentType2] = fields::instrument_type2(inst);
    r[row::ISIN] = field(raw, "ISIN");
    r[row::multiplier] = std::to_string(c.scale);
    std::int64_t lot = to_int(raw.count("NewBrdLotQty") ? raw.at("NewBrdLotQty") : "None", 1);
    r[row::lotSize] = std::to_string(lot ? lot : 1);
    r[row::tickSize] = std::to_string(to_int(raw.count("BidIntrvl") ? raw.at("BidIntrvl") : "None", 0));
    r[row::tradingSessionUTC] = c.session;
    r[row::expiration] = std::to_string(expiration_ns(xpry, segment));
    r[row::underlying] = ticker;
    r[row::underlying_root] = ticker;
    r[row::strike] = std::to_string(strike_raw > 0 ? strike_raw : 0);
    r[row::optionType] = option == "CE" ? "CALL" : option == "PE" ? "PUT" : "";
    r[row::currency] = "INR";
    r[row::brokerScript1] = broker_script::from_equity(script);
    row::fill_unspecified_broker_scripts(r);
    row::fill_flags(r);
    return r;
}

std::vector<Raw> read_csv(const std::filesystem::path& path) {
    std::string text = util::utf8_replace_invalid(util::read_text_utf8_sig(path));
    auto records = csv::parse_records(text);
    std::vector<Raw> rows;
    if (records.empty()) return rows;
    std::size_t header = 0;
    while (header < records.size() && records[header].empty()) ++header;
    if (header >= records.size()) return rows;
    const auto& names = records[header];
    for (std::size_t i = header + 1; i < records.size(); ++i) {
        const auto& rec = records[i];
        if (rec.empty()) continue;
        Raw r;
        for (std::size_t c = 0; c < names.size() && c < rec.size(); ++c) r[names[c]] = rec[c];
        rows.push_back(std::move(r));
    }
    return rows;
}

std::pair<std::vector<row::Row>, std::vector<std::pair<std::string, std::size_t>>> rows_for(const std::string& as_of) {
    auto dir = drop_dir(as_of);
    std::vector<row::Row> rows;
    std::vector<std::pair<std::string, std::size_t>> counts;
    std::map<std::string, std::int64_t> unknown_series;
    std::vector<std::string> unknown_order;
    auto cash = read_csv(dir / CM_FILE);
    auto by_isin = types_by_isin(cash);
    for (const auto& raw : cash) {
        auto mapped = map_cash_row(raw, &by_isin);
        if (!mapped) continue;
        std::string series = util::upper(field(raw, "SctySrs"));
        auto isin_type = by_isin.find(field(raw, "ISIN"));
        bool known_isin = isin_type != by_isin.end() && !isin_type->second.empty();
        if (!kSeriesTypes.count(series) && !kSeriesEquity.count(series) && !kSeriesByFlag.count(series) && !known_isin) {
            if (!unknown_series.count(series)) unknown_order.push_back(series);
            unknown_series[series] += 1;
        }
        rows.push_back(std::move(*mapped));
    }
    counts.push_back({CM_FILE, rows.size()});
    for (auto [name, segment] : {std::pair{FO_FILE, Segment::Deriv}, std::pair{CD_FILE, Segment::Currency}}) {
        std::size_t before = rows.size();
        for (const auto& raw : read_csv(dir / name))
            if (auto mapped = map_derivative_row(raw, segment)) rows.push_back(std::move(*mapped));
        counts.push_back({name, rows.size() - before});
    }
    if (!unknown_series.empty()) {
        std::vector<std::pair<std::string, std::int64_t>> top;
        for (const auto& s : unknown_order) top.push_back({s, unknown_series[s]});
        std::stable_sort(top.begin(), top.end(), [](const auto& a, const auto& b) { return a.second > b.second; });
        if (top.size() > 8) top.resize(8);
        std::int64_t total = 0;
        for (const auto& [s, n] : unknown_series) total += n;
        std::vector<std::string> parts;
        for (const auto& [s, n] : top) parts.push_back(s + "=" + util::commas(n));
        log::out("    " + util::commas(total) + " row(s) in " + std::to_string(unknown_series.size()) +
                 " series named by neither the series table nor a matching ISIN, defaulted to " + kSeriesDefault + ": " +
                 util::join(parts, ", "));
    }
    std::set<std::string> seen;
    std::vector<row::Row> unique;
    std::int64_t collisions = 0;
    for (auto& r : rows) {
        if (!seen.insert(r[row::script]).second) {
            ++collisions;
            continue;
        }
        unique.push_back(std::move(r));
    }
    if (collisions)
        log::out("    " + util::commas(collisions) +
                 " row(s) dropped: a script already claimed by an earlier row -- one token per script");
    return {std::move(unique), counts};
}

void run(runner::Opts& opts) {
    auto& numbering = numbering_session::of(opts);
    if (opts.dry_run && !numbering.plans_preview()) {
        log::out("DRY RUN: Would normalize NSE contract masters");
        return;
    }
    if (!config::owns("XNSE", "nse")) {
        log::out("  Skipping XNSE contract masters: feed = " + config::feed_for("XNSE"));
        return;
    }
    if (!runner::venue_selected(opts, "XNSE")) return;
    if (!present(opts.date_dir)) {
        log::out("  No NSE contract drop for XNSE (" + drop_dir(opts.date_dir).string() + ")");
        numbering.no_input(opts.date_dir, "XNSE", "no contract drop in " + drop_dir(opts.date_dir).string());
        return;
    }
    const auto* cfg = counter_token::exchange_for("XNSE");
    if (cfg == nullptr || !cfg->venue_id) {
        log::out("  CRITICAL: skipping XNSE -- no venue_id in [venue.XNSE]");
        numbering.record(opts.date_dir, "XNSE", "refused", "no venue_id in [venue.XNSE]");
        return;
    }
    auto token_errors = counter_token::validate(config::get().venues);
    if (auto it = token_errors.find("XNSE"); it != token_errors.end()) {
        for (const auto& msg : it->second) log::out("  CRITICAL [XNSE] counterToken config: " + msg);
        numbering.record(opts.date_dir, "XNSE", "refused", "counterToken config: " + util::join(it->second, "; "));
        return;
    }
    log::out("  Normalizing NSE contract masters...");
    std::string started_at = counter_token::utc_now();
    auto [rows, counts] = rows_for(opts.date_dir);
    for (const auto& [name, n] : counts)
        log::out("    " + name + ": " + util::commas(static_cast<std::int64_t>(n)) + " instrument(s)");
    if (rows.empty()) {
        log::out("    No instruments -- nothing written");
        return;
    }
    for (std::size_t n = 0; n < rows.size(); ++n) rows[n][row::counterToken] = std::to_string(n + 1);
    std::optional<numbering_session::VenueNumbering> venue_numbering;
    std::optional<numbering::Plan> plan;
    try {
        venue_numbering.emplace(numbering.venue(opts.date_dir, "XNSE", cfg->venue_id));
        std::vector<std::string> scripts;
        for (const auto& r : rows) scripts.push_back(r[row::script]);
        plan = venue_numbering->plan(scripts);
    } catch (const ValueError& exc) {
        log::out(std::string("  CRITICAL: skipping XNSE -- ") + exc.what());
        numbering.record(opts.date_dir, "XNSE", "refused", exc.what());
        return;
    }
    for (auto& r : rows) r[row::counterTokenV2] = plan->rendered(r[row::script]);
    log::out("    XNSE counterTokenV2: " + venue_numbering->describe() + "; " + venue_numbering->summary(*plan));
    if (venue_numbering->preview()) {
        venue_numbering->commit(*plan);
        return;
    }
    venue_numbering->reserve(*plan);
    auto output = paths::normalized_dir(opts.date_dir) / OUTPUT;
    std::filesystem::create_directories(output.parent_path());
    parquet::write_rows(output, paths::NORMALIZED_COLUMNS, rows);
    auto dir = drop_dir(opts.date_dir);
    std::vector<counter_token::Artifact> inputs;
    for (const char* name : {CM_FILE, FO_FILE, CD_FILE}) inputs.push_back(counter_token::artifact(dir / name, opts.date_dir));
    venue_numbering->commit(*plan, started_at, inputs,
                            {counter_token::artifact(output, opts.date_dir, static_cast<std::int64_t>(rows.size()))});
    log::out("    Wrote " + util::commas(static_cast<std::int64_t>(rows.size())) + " rows to " + output.filename().string());
}

}  // namespace premarket::nse_contract
