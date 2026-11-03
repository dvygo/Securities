#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include <databento/record.hpp>

#include "premarket/normalize/broker_script.hpp"
#include "premarket/normalize/row.hpp"
#include "premarket/normalize/session.hpp"
#include "premarket/runner.hpp"

namespace premarket::databento_norm {

inline constexpr std::size_t NORMALIZE_CHUNK_ROWS = 50000;
inline constexpr char XCBO_SPOT_CLASS = 'K';

struct DefRow {
    std::uint32_t instrument_id = 0;
    std::string raw_symbol;
    std::string instrument_class;
    std::vector<std::string> fields;
};

std::vector<std::string> definition_values(const databento::InstrumentDefMsg& rec);
DefRow def_row(const databento::InstrumentDefMsg& rec);

std::string script_of(const DefRow& r);
std::optional<broker_script::Occ> parse_occ_symbol(const std::string& symbol);
std::int64_t glbx_expiration_ns(const std::string& symbol_base, const session::Ymd& ref, std::int64_t anchor_ns = 0);
std::string underlying_root_from_stype_in(const std::string& symbol);
std::string dotted_underlying(const std::string& root);
void reset_dotted_cache();

row::Row map_xcme_row(const DefRow& r, const session::Ymd& ref);
row::Row map_xcbo_row(const DefRow& r, const session::Ymd& ref);
row::Row map_xnas_row(const DefRow& r, const session::Ymd& ref);

std::vector<std::filesystem::path> definition_files(const std::filesystem::path& dir);
void for_each_definition(const std::vector<std::filesystem::path>& files, const std::string& dataset,
                         const std::function<void(const databento::InstrumentDefMsg&)>& fn, bool announce_skips = true);

void run(runner::Opts& opts);

}  // namespace premarket::databento_norm
