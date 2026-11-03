#include "premarket/paths.hpp"

#include <cerrno>
#include <cstring>

#include "premarket/config.hpp"
#include "premarket/util.hpp"

namespace premarket::paths {

fs::path data_root() { return config::get().paths.data_dir; }
fs::path reports_dir() { return config::get().paths.reports_dir; }
fs::path sinks_dir() { return config::get().paths.sinks_dir; }

fs::path day_dir(const std::string& as_of) { return data_root() / as_of / TRANSFORM_DIR; }
fs::path venue_dir(const std::string& as_of, const std::string& venue_name) {
    return data_root() / as_of / util::upper(venue_name);
}
fs::path manual_venue_dir(const std::string& as_of, const std::string& venue_name) {
    return data_root() / as_of / util::upper(venue_name);
}

fs::path fyers_segment_path(const std::string& as_of, const std::string& segment) {
    return venue_dir(as_of, fyers_segment_mic(segment)) / fyers_segment(segment).local_file;
}

fs::path normalized_dir(const std::string& as_of) { return day_dir(as_of) / "normalized"; }
fs::path plugin_dir(const std::string& as_of) { return day_dir(as_of) / "plugin"; }
fs::path baskets_dir() { return config::get().paths.baskets_dir; }
fs::path contracts_dir() { return config::get().paths.contracts_dir; }

fs::path contracts_day_dir(const std::string& as_of) { return contracts_dir() / as_of; }

fs::path logs_dir() { return config::get().paths.logs_dir; }

void ensure_log_dir() { fs::create_directories(logs_dir()); }

std::string validate_date_dir(const std::string& date_dir) {
    if (date_dir.size() < 8 || !util::is_digits(date_dir.substr(0, 8)) ||
        (date_dir.size() > 8 && !(date_dir.size() == 9 && date_dir[8] == '\n')))
        throw ValueError("Invalid date_dir format (must be YYYYMMDD): " + date_dir);
    return date_dir;
}

void promote_staging(const fs::path& temp_path, const fs::path& output_path) {
    if (std::rename(temp_path.c_str(), output_path.c_str()) != 0) {
        if (errno == EACCES || errno == EPERM || errno == EBUSY)
            throw RuntimeError("cannot replace " + output_path.filename().string() +
                               ": it is open in another program (Excel locks CSVs exclusively). Close it "
                               "and re-run; the finished output is already staged at " +
                               temp_path.filename().string() + " -- moving that file over " +
                               output_path.filename().string() + " by hand is equivalent. Original error: " +
                               std::strerror(errno));
        throw RuntimeError(std::string("cannot replace ") + output_path.string() + ": " + std::strerror(errno));
    }
}

const std::vector<std::string> NEXUS_BASKET_COLUMNS = {
    "script", "scriptToken", "scriptInstrumentType2", "optionType", "underlying_root", "underlying",
    "strike", "expiration", "multiplier", "currency", "exchange",
};

const std::vector<std::string> DEFINITION_FIELDS = {
    "ts_recv", "ts_event", "rtype", "publisher_id", "security_update_action", "raw_instrument_id",
    "underlying_id", "min_price_increment", "display_factor", "high_limit_price", "low_limit_price",
    "max_price_variation", "unit_of_measure_qty", "min_price_increment_amount", "price_ratio",
    "strike_price", "expiration", "activation", "inst_attrib_value", "market_depth_implied",
    "market_depth", "market_segment_id", "max_trade_vol", "min_lot_size", "min_lot_size_block",
    "min_lot_size_round_lot", "min_trade_vol", "contract_multiplier", "decay_quantity",
    "original_contract_size", "appl_id", "maturity_year", "maturity_month", "maturity_day",
    "maturity_week", "decay_start_date", "channel_id", "currency", "settl_currency", "secsubtype",
    "group", "exchange", "asset", "cfi", "security_type", "unit_of_measure", "underlying",
    "strike_price_currency", "match_algorithm", "user_defined_instrument", "main_fraction",
    "price_display_format", "sub_fraction", "underlying_product", "contract_multiplier_unit",
    "flow_schedule_type", "tick_rule", "leg_count", "leg_index", "leg_instrument_id",
    "leg_raw_symbol", "leg_instrument_class", "leg_side", "leg_price", "leg_delta",
    "leg_ratio_price_numerator", "leg_ratio_price_denominator", "leg_ratio_qty_numerator",
    "leg_ratio_qty_denominator", "leg_underlying_id",
};

namespace {

std::vector<std::string> prefixed_definition_columns() {
    std::vector<std::string> out;
    for (const auto& f : DEFINITION_FIELDS) out.push_back(DEFINITION_COLUMN_PREFIX + f);
    return out;
}

std::vector<std::string> normalized_columns() {
    std::vector<std::string> out = {
        "scriptDetails", "scriptInstrumentType", "scriptInstrumentType2", "multiplier", "lotSize",
        "tickSize", "ISIN", "tradingSessionUTC", "expiration", "script", "scriptToken",
        "underlying_root", "underlying", "strike", "optionType", "currency", "brokerScript1",
        "brokerScript2", "brokerScript3", "brokerScript4", "counterToken", "counterTokenV2",
    };
    out.insert(out.end(), ENABLE_COLUMNS.begin(), ENABLE_COLUMNS.end());
    out.insert(out.end(), DEFINITION_PASSTHROUGH_COLUMNS.begin(), DEFINITION_PASSTHROUGH_COLUMNS.end());
    return out;
}

std::vector<std::string> contract_columns() {
    std::vector<std::string> out = {"date", "exchange"};
    out.insert(out.end(), NORMALIZED_COLUMNS.begin(), NORMALIZED_COLUMNS.end());
    return out;
}

}  // namespace

const std::vector<std::string> DEFINITION_PASSTHROUGH_COLUMNS = prefixed_definition_columns();
const std::vector<std::string> ENABLE_COLUMNS = {"enable1", "enable2"};
const std::vector<std::string> NORMALIZED_COLUMNS = normalized_columns();
const std::vector<std::string> CONTRACT_COLUMNS = contract_columns();

const std::vector<FyersSegment> FYERS_RAW_SEGMENTS = {
    {"xnse", "NSE_CM.csv", "XNSE-FYERS.csv"}, {"xnfo", "NSE_FO.csv", "XNFO-FYERS.csv"},
    {"xncd", "NSE_CD.csv", "XNCD-FYERS.csv"}, {"xbse", "BSE_CM.csv", "XBSE-FYERS.csv"},
    {"xbfo", "BSE_FO.csv", "XBFO-FYERS.csv"}, {"xmcx", "MCX_COM.csv", "XMCX-FYERS.csv"},
};

const std::vector<FyersBundle> FYERS_MIC_BUNDLES = {
    {"XNSE", "XNSE-FYERS.parquet", "xnse", {"XNSE-FYERS.csv", "XNFO-FYERS.csv", "XNCD-FYERS.csv"}},
    {"XBOM", "XBOM-FYERS.parquet", "xbom", {"XBSE-FYERS.csv", "XBFO-FYERS.csv"}},
    {"XIMC", "XIMC-FYERS.parquet", "ximc", {"XMCX-FYERS.csv"}},
};

const std::map<std::string, std::map<std::string, std::string>>& feed_outputs() {
    static const auto table = [] {
        std::map<std::string, std::map<std::string, std::string>> t;
        for (const auto& b : FYERS_MIC_BUNDLES) t[b.mic]["fyers"] = b.output;
        t["XNSE"]["nse"] = "XNSE-NSE.parquet";
        t["XIMC"]["mcx"] = "XIMC-MCX.parquet";
        return t;
    }();
    return table;
}

const FyersSegment& fyers_segment(const std::string& segment) {
    for (const auto& s : FYERS_RAW_SEGMENTS)
        if (s.segment == segment) return s;
    throw ValueError("'" + segment + "'");
}

std::string fyers_segment_mic(const std::string& segment) {
    const auto& local = fyers_segment(segment).local_file;
    for (const auto& b : FYERS_MIC_BUNDLES)
        for (const auto& src : b.sources)
            if (src == local) return b.mic;
    throw RuntimeError("Fyers segment(s) ['" + segment +
                       "'] are in FYERS_RAW_SEGMENTS but no FYERS_MIC_BUNDLES entry lists their file.");
}

const std::vector<FuturesSource> INDEX_FUTURES_SOURCES = {};
const std::vector<FuturesSource> EQUITY_FUTURES_SOURCES = {
    {"XNSE_NIFTYFNO_FUTURES_NEAR", "XNSE_NIFTYFNO_EQUITY", true},
    {"XNSE_NIFTYFNO_FUTURES_ALL", "XNSE_NIFTYFNO_EQUITY", false},
};
const std::vector<std::pair<std::string, std::string>> EQUITY_BASKETS = {{"XNSE_NIFTYFNO_EQUITY", "XNSE"}};
const std::vector<OptionBasketSource> OPTION_BASKET_SOURCES = {};
const std::vector<std::string> ALL_INDEX_FUTURES_PARTS = {};

const std::vector<std::string>& basket_names() {
    static const auto names = [] {
        std::vector<std::string> out;
        for (const auto& [name, mic] : EQUITY_BASKETS) out.push_back(name);
        if (!ALL_INDEX_FUTURES_PARTS.empty()) out.push_back("ALL_INDEX_FUTURES");
        for (const auto& f : INDEX_FUTURES_SOURCES) out.push_back(f.name);
        for (const auto& f : EQUITY_FUTURES_SOURCES) out.push_back(f.name);
        for (const auto& o : OPTION_BASKET_SOURCES) out.push_back(o.name);
        return out;
    }();
    return names;
}

std::string options_basket_name(const std::string& futures) {
    auto pos = futures.find('_');
    if (pos == std::string::npos) return futures + "_OPTIONS_";
    return futures.substr(0, pos) + "_OPTIONS_" + futures.substr(pos + 1);
}

}  // namespace premarket::paths
