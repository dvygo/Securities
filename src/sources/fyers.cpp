#include "premarket/sources/fyers.hpp"

#include <httplib.h>

#include <chrono>
#include <thread>

#include "premarket/csv.hpp"
#include "premarket/log.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::fyers {

const std::vector<std::string> FYERS_COLUMNS = {
    "fyToken", "symDetails", "exInstType", "minLotSize", "tickSize", "isin", "tradingSession",
    "lastUpdate", "expiryDate", "symTicker", "exchange", "segment", "exToken", "exSymName",
    "underExToken", "strikePrice", "optType", "underFyTok", "underSym", "fyersExtra1", "fyersExtra2",
};

const std::map<std::string, std::string> LEGACY_HEADER_ALIASES = {
    {"fytoken", "fyToken"},          {"symbol", "symDetails"},     {"instrumenttype", "exInstType"},
    {"lotsize", "minLotSize"},       {"isin", "isin"},             {"symbolticker", "symTicker"},
    {"scriptcode", "exToken"},       {"scripcode", "exToken"},     {"scripname", "exSymName"},
    {"shortsym", "exSymName"},       {"scriptoken", "underExToken"}, {"optiontype", "optType"},
    {"underfytoken", "underFyTok"},  {"underexsymbol", "underSym"},
};

const std::map<int, std::string> EXCHANGE_CODES = {{10, "NSE"}, {11, "MCX"}, {12, "BSE"}};
const std::map<int, std::string> SEGMENT_CODES = {{10, "CM"}, {11, "FO"}, {12, "CD"}, {20, "COM"}};
const std::map<int, std::string> INSTRUMENT_CODES = {
    {0, "EQ"},        {1, "PREFSHARES"}, {2, "DEBENTURES"}, {3, "WARRANTS"}, {4, "MISC"},     {5, "SGB"},
    {6, "G-SECS"},    {7, "T-BILLS"},    {8, "MF"},         {9, "ETF"},      {10, "INDEX"},   {11, "FUTIDX"},
    {12, "FUTIVX"},   {13, "FUTSTK"},    {14, "OPTIDX"},    {15, "OPTSTK"},  {16, "FUTCUR"},  {17, "FUTIRT"},
    {18, "FUTIRC"},   {19, "OPTCUR"},    {20, "UNDCUR"},    {21, "UNDIRC"},  {22, "UNDIRT"},  {23, "UNDIRD"},
    {24, "INDEX_CD"}, {25, "FUTIRD"},    {30, "FUTCOM"},    {31, "OPTFUT"},  {32, "OPTCOM"},  {33, "FUTBAS"},
    {34, "FUTBLN"},   {35, "FUTENR"},    {36, "OPTBLN"},    {37, "OPTFUT_NCOM"}, {50, "MISC_BSE"},
};

namespace {

const std::map<std::pair<int, int>, std::string> kExchangeMic = {
    {{10, 10}, "XNSE"}, {{10, 11}, "XNFO"}, {{10, 12}, "XNCD"}, {{10, 20}, "XNCO"},
    {{12, 10}, "XBSE"}, {{12, 11}, "XBFO"}, {{12, 12}, "XBCD"}, {{11, 20}, "XMCX"},
};

}  // namespace

std::string normalize_header_key(const std::string& raw) {
    std::string key = util::strip(raw);
    std::string lowered = util::lower(key);
    if (auto it = LEGACY_HEADER_ALIASES.find(lowered); it != LEGACY_HEADER_ALIASES.end()) return it->second;
    for (const auto& col : FYERS_COLUMNS)
        if (key == col || lowered == util::lower(col)) return col;
    return key;
}

std::string resolve_exchange_mic(const std::string& exchange, const std::string& segment) {
    auto e = util::parse_int(exchange), s = util::parse_int(segment);
    if (!e || !s) return "";
    auto it = kExchangeMic.find({static_cast<int>(*e), static_cast<int>(*s)});
    return it == kExchangeMic.end() ? "" : it->second;
}

std::vector<Row> read_raw_csv(const std::filesystem::path& path) {
    std::error_code ec;
    if (!std::filesystem::exists(path, ec)) return {};
    std::string text = util::strip(util::read_text_utf8_sig(path));
    auto lines = util::split(text, "\n");
    bool has_header = false;
    for (const auto& part : util::split(lines.front(), ","))
        if (LEGACY_HEADER_ALIASES.count(util::lower(part))) has_header = true;
    std::vector<std::string> fieldnames = FYERS_COLUMNS;
    std::size_t first = 0;
    if (has_header) {
        fieldnames = csv::parse_line(lines.front());
        first = 1;
    }
    std::vector<Row> rows;
    for (std::size_t i = first; i < lines.size(); ++i) {
        auto fields = csv::parse_line(lines[i]);
        if (fields.empty()) continue;
        if (fields.size() > fieldnames.size())
            throw ValueError(path.filename().string() + ": line " + std::to_string(i + 1) + " has " +
                             std::to_string(fields.size()) + " fields, the header " + std::to_string(fieldnames.size()));
        Row row;
        bool any = false;
        for (std::size_t c = 0; c < fields.size(); ++c) {
            if (!fields[c].empty()) any = true;
            row[fieldnames[c]] = fields[c];
        }
        if (!any) continue;
        rows.push_back(std::move(row));
    }
    return rows;
}

std::vector<Row> parse_fyers_csv(const std::filesystem::path& path) {
    auto rows = read_raw_csv(path);
    std::vector<Row> out;
    out.reserve(rows.size());
    for (auto& row : rows) {
        Row norm;
        for (auto& [k, v] : row) norm[normalize_header_key(k)] = std::move(v);
        out.push_back(std::move(norm));
    }
    return out;
}

std::string fetch(const std::string& url, const config::Fyers& cfg) {
    auto scheme_end = url.find("://");
    auto path_start = url.find('/', scheme_end == std::string::npos ? 0 : scheme_end + 3);
    std::string origin = url.substr(0, path_start);
    std::string path = path_start == std::string::npos ? "/" : url.substr(path_start);
    std::string last_error;
    for (std::int64_t attempt = 1; attempt <= cfg.retries; ++attempt) {
        httplib::Client client(origin);
        client.set_follow_location(true);
        client.set_connection_timeout(static_cast<time_t>(cfg.timeout_sec), 0);
        client.set_read_timeout(static_cast<time_t>(cfg.timeout_sec), 0);
        client.set_default_headers({{"User-Agent", cfg.user_agent}});
        auto res = client.Get(path);
        if (res && res->status >= 200 && res->status < 300) return res->body;
        last_error = res ? "HTTP " + std::to_string(res->status) + " for url: " + url
                         : httplib::to_string(res.error()) + " for url: " + url;
        if (attempt < cfg.retries) std::this_thread::sleep_for(std::chrono::seconds(cfg.retry_delay_sec));
    }
    throw RuntimeError(last_error);
}

void download(runner::Opts& opts) {
    const auto& cfg = config::get().fyers;
    if (opts.dry_run) {
        log::out("DRY RUN: Would download Fyers symbol master data");
        return;
    }
    for (const auto& seg : paths::FYERS_RAW_SEGMENTS) {
        std::string mic = paths::fyers_segment_mic(seg.segment);
        if (!config::owns(mic, "fyers")) {
            std::string feed = config::feed_for(mic);
            log::out("Skipping " + seg.segment + ": " + mic + " is on the " + util::quoted(feed) + " feed, not Fyers");
            continue;
        }
        std::string url = cfg.base_url + "/" + seg.vendor_file;
        auto output = paths::fyers_segment_path(opts.date_dir, seg.segment);
        std::filesystem::create_directories(output.parent_path());
        log::out("Downloading " + seg.segment + "...");
        try {
            std::string body = fetch(url, cfg);
            auto staging = util::staging_path(output);
            util::write_file(staging, body);
            util::replace_file(staging, output);
            log::out("  Wrote " + output.string());
        } catch (const std::exception& e) {
            log::out("  Error downloading " + seg.segment + ": " + e.what());
        }
    }
}

}  // namespace premarket::fyers
