#include "premarket/plugin.hpp"

#include <unistd.h>

#include <cstring>
#include <fstream>
#include <set>

#include "premarket/config.hpp"
#include "premarket/export.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/session.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::plugin {

const std::vector<std::string> PLUGIN_COLUMNS = {
    "trade_date", "segment", "token",      "symbol", "expirydate", "insttype", "optiontype", "strikeprice", "lotmultiple",
    "lotsize",    "ticksize", "name",      "series", "divisor",    "exch",     "fullname",   "freeze_qty",
};

namespace {

const std::string& get(const Normalized& r, const std::string& key) {
    static const std::string empty;
    auto it = r.find(key);
    return it == r.end() ? empty : it->second;
}

std::int64_t price_value(const std::string& value) {
    if (value.empty()) return 0;
    std::int64_t price;
    if (auto i = util::parse_int(value)) price = *i;
    else if (auto f = util::parse_int_truncating(value)) price = *f;
    else return 0;
    return (price <= 0 || price >= UNDEF_PRICE) ? 0 : price;
}

bool is_missing(const std::string& column, const std::string& value) {
    if (util::strip(value).empty()) return true;
    if (column == "strikeprice") {
        auto f = util::parse_float(value);
        return f && *f == 0;
    }
    return false;
}

std::string fill(const std::string& column, const std::string& value) {
    if (!is_missing(column, value)) return value;
    if (column == "strikeprice") return "-1";
    if (column == "optiontype") return "XX";
    if (column == "segment") return "F&O";
    return "1";
}

}  // namespace

std::int64_t expiry_ns(const Normalized& row) {
    std::int64_t def = session::as_ns(get(row, "def_expiration"));
    return def ? def : session::as_ns(get(row, "expiration"));
}

bool is_expired(const Normalized& row, std::int64_t cutoff_ns) {
    std::int64_t ns = expiry_ns(row);
    return 0 < ns && ns < cutoff_ns;
}

std::string ticksize(const Normalized& row) {
    const std::string& canonical = get(row, "tickSize");
    if (!util::strip(canonical).empty()) return canonical;
    std::int64_t p = price_value(get(row, "def_min_price_increment"));
    return p ? std::to_string(p) : "";
}

std::string fullname(const std::string& inst_type2, const std::string& underlying, const std::string& strike,
                     const std::string& divisor, const std::string& opt_code, const std::string& expiry_str) {
    if (inst_type2 == "OPTION") {
        std::string display = strike;
        if (!divisor.empty() && divisor != "0") {
            auto s = util::parse_int_truncating(strike);
            auto d = util::parse_int_truncating(divisor);
            if (s && d && *d != 0) display = util::fixed(static_cast<double>(*s) / static_cast<double>(*d), 2);
        }
        return util::strip("OPT " + underlying + " " + display + " " + opt_code + " " + expiry_str);
    }
    if (inst_type2 == "FUTURE") return util::rstrip("FUT " + underlying + "  " + expiry_str);
    return underlying;
}

PluginRow map_row(const Normalized& row, const std::string& trade_date, const std::string& exchange) {
    const std::string& type2 = get(row, "scriptInstrumentType2");
    const std::string& option_type = get(row, "optionType");
    std::string opt_code = option_type == "CALL" ? "CE" : option_type == "PUT" ? "PE" : "";
    std::string series = type2 == "FUTURE" ? "XX" : !opt_code.empty() ? opt_code : "";
    std::int64_t expiry_sec = expiry_ns(row) / 1000000000LL;
    std::string expiry_str = expiry_sec ? util::utc_format_ns(expiry_sec * 1000000000LL, "%Y-%m-%d") : "";
    std::string segment = type2 == "FUTURE" || type2 == "OPTION" ? "F&O" : type2 == "EQUITY" ? "CM" : "";
    std::string token = get(row, "counterTokenV2");
    if (token.empty()) token = get(row, "counterToken");
    if (token.empty()) token = get(row, "scriptToken");
    PluginRow out = {
        trade_date,
        segment,
        token,
        get(row, "underlying_root"),
        std::to_string(expiry_sec),
        get(row, "scriptInstrumentType"),
        opt_code,
        get(row, "strike"),
        "",
        get(row, "lotSize"),
        ticksize(row),
        get(row, "script"),
        series,
        get(row, "multiplier"),
        exchange,
        fullname(type2, get(row, "underlying"), row.count("strike") ? get(row, "strike") : "0", get(row, "multiplier"),
                 opt_code, expiry_str),
        "",
    };
    for (std::size_t i = 0; i < out.size(); ++i) out[i] = fill(PLUGIN_COLUMNS[i], out[i]);
    return out;
}

std::vector<std::filesystem::path> build_day(const std::string& date_dir, const std::vector<std::string>& markets) {
    log::out("  Building plugin files...");
    auto normalized = exporter::normalized_files(date_dir);
    if (normalized.empty()) {
        log::out("    No normalized files found");
        return {};
    }
    std::set<std::string> wanted;
    for (const auto& m : markets) wanted.insert(util::upper(m));
    std::string trade_date = date_dir.substr(0, 4) + "-" + date_dir.substr(4, 2) + "-" + date_dir.substr(6, 2);
    std::int64_t cutoff = util::day_start_ns(date_dir);
    auto plugin_dir = paths::plugin_dir(date_dir);
    std::filesystem::create_directories(plugin_dir);
    std::vector<std::filesystem::path> written;
    for (const auto& src : normalized) {
        std::string exchange = exporter::exchange_of(src);
        if (!wanted.count(util::upper(exchange))) continue;
        const auto* cfg = config::venue(exchange);
        if (cfg != nullptr && !cfg->enabled) {
            log::out("    Skipping " + exchange + ": enabled = 0");
            continue;
        }
        auto output = plugin_dir / src.filename();
        parquet::RowWriter writer(output, PLUGIN_COLUMNS);
        std::size_t total = 0, expired = 0;
        try {
            parquet::iter_rows(src, PLUGIN_CHUNK_ROWS, [&](const std::vector<std::string>& columns,
                                                          const std::vector<parquet::Row>& rows) {
                std::vector<PluginRow> batch;
                for (const auto& raw : rows) {
                    Normalized r;
                    for (std::size_t c = 0; c < columns.size(); ++c) r[columns[c]] = raw[c];
                    if (get(r, "scriptToken").empty()) continue;
                    if (is_expired(r, cutoff)) {
                        ++expired;
                        continue;
                    }
                    batch.push_back(map_row(r, trade_date, exchange));
                }
                if (batch.empty()) return;
                writer.write(batch);
                total += batch.size();
            });
        } catch (const std::exception& e) {
            log::out("    Error processing " + src.string() + ": " + e.what());
            writer.abort();
            continue;
        }
        std::string dropped = expired ? " (" + util::commas(static_cast<std::int64_t>(expired)) + " expired dropped)" : "";
        if (writer.close()) {
            written.push_back(output);
            log::out("    Wrote " + std::to_string(total) + " rows to " + output.string() + dropped);
        } else {
            log::out("    No plugin rows for " + exchange + dropped);
        }
    }
    return written;
}

namespace tokenmap {

namespace {

constexpr std::uint64_t kFnvOffset = 0xCBF29CE484222325ULL;
constexpr std::uint64_t kFnvPrime = 0x100000001B3ULL;

void put32(std::string& b, std::size_t at, std::uint32_t v) {
    for (int i = 0; i < 4; ++i) b[at + static_cast<std::size_t>(i)] = static_cast<char>((v >> (8 * i)) & 0xFF);
}
void put64(std::string& b, std::size_t at, std::uint64_t v) {
    for (int i = 0; i < 8; ++i) b[at + static_cast<std::size_t>(i)] = static_cast<char>((v >> (8 * i)) & 0xFF);
}
std::uint32_t get32(const std::string& b, std::size_t at) {
    std::uint32_t v = 0;
    for (int i = 3; i >= 0; --i) v = (v << 8) | static_cast<unsigned char>(b[at + static_cast<std::size_t>(i)]);
    return v;
}
std::uint64_t get64(const std::string& b, std::size_t at) {
    std::uint64_t v = 0;
    for (int i = 7; i >= 0; --i) v = (v << 8) | static_cast<unsigned char>(b[at + static_cast<std::size_t>(i)]);
    return v;
}

std::optional<std::int64_t> as_int(const std::string& raw) {
    std::string t = util::strip(raw);
    if (t.empty()) return std::nullopt;
    return util::parse_int(t);
}

}  // namespace

std::string Skips::describe() const {
    std::vector<std::string> parts;
    if (unparseable_id) parts.push_back(util::commas(unparseable_id) + " unparseable instrument_id");
    if (unparseable_token) parts.push_back(util::commas(unparseable_token) + " unparseable token");
    if (reserved_id) parts.push_back(util::commas(reserved_id) + " instrument_id 0 (reserved)");
    if (id_out_of_u32) parts.push_back(util::commas(id_out_of_u32) + " instrument_id outside u32");
    if (token_out_of_range) parts.push_back(util::commas(token_out_of_range) + " token outside 1..2,147,483,647");
    return parts.empty() ? "none" : util::join(parts, ", ");
}

std::string filename(const std::string& venue) { return "tokenmap." + util::upper(venue) + ".bin"; }

std::uint64_t fnv1a(const std::string& body) {
    std::uint64_t h = kFnvOffset;
    for (unsigned char c : body) h = (h ^ c) * kFnvPrime;
    return h;
}

std::string key_column(const std::vector<std::map<std::string, std::string>>& rows) {
    std::size_t populated = 0;
    for (const auto& r : rows) {
        auto it = r.find("def_raw_instrument_id");
        auto v = as_int(it == r.end() ? "" : it->second);
        if (v && *v) ++populated;
    }
    if (populated == 0) return "scriptToken";
    if (populated == rows.size()) return "def_raw_instrument_id";
    throw ValueError("def_raw_instrument_id is populated on " + util::commas(static_cast<std::int64_t>(populated)) +
                     " of " + util::commas(static_cast<std::int64_t>(rows.size())) +
                     " row(s). Precedence cannot choose between it and scriptToken, and guessing produces a map that "
                     "loads cleanly while resolving instruments to the wrong tokens. Fix the master, or decide the "
                     "column deliberately.");
}

Built build(const std::string& venue, const std::vector<std::map<std::string, std::string>>& rows, const std::string& key) {
    Built out;
    out.venue = util::upper(venue);
    out.source_rows = rows.size();
    out.key = key.empty() ? key_column(rows) : key;
    std::map<std::uint32_t, std::int32_t> seen;
    for (const auto& r : rows) {
        auto it = r.find(out.key);
        auto id = as_int(it == r.end() ? "" : it->second);
        if (!id) {
            ++out.skips.unparseable_id;
            continue;
        }
        if (*id == 0) {
            ++out.skips.reserved_id;
            continue;
        }
        if (!(*id > 0 && *id <= 0xFFFFFFFFLL)) {
            ++out.skips.id_out_of_u32;
            continue;
        }
        auto tv = r.find("counterTokenV2");
        auto token = as_int(tv == r.end() ? "" : tv->second);
        if (!token) {
            ++out.skips.unparseable_token;
            continue;
        }
        if (*token < 1 || *token > 2147483647LL) {
            ++out.skips.token_out_of_range;
            continue;
        }
        auto key32 = static_cast<std::uint32_t>(*id);
        auto tok32 = static_cast<std::int32_t>(*token);
        auto prev = seen.find(key32);
        if (prev != seen.end() && prev->second != tok32)
            throw ValueError(out.venue + ": instrument_id " + std::to_string(key32) + " maps to both token " +
                             std::to_string(prev->second) + " and " + std::to_string(tok32) +
                             ". The loader requires strictly ascending ids, so one of them would have to be dropped -- "
                             "which one wins would depend on row order. Fix the master.");
        seen[key32] = tok32;
    }
    out.entries.assign(seen.begin(), seen.end());
    return out;
}

std::string encode(const Built& built, std::optional<std::uint64_t> built_at_ns) {
    if (built.entries.empty())
        throw ValueError(built.venue + ": no entries survived, and entry_count == 0 is a hard load failure. " +
                         util::commas(static_cast<std::int64_t>(built.source_rows)) + " source row(s), skipped: " +
                         built.skips.describe() + ".");
    if (built.venue.size() > 8) throw ValueError("venue " + util::quoted(built.venue) + " exceeds 8 ASCII bytes");
    std::string body(built.entries.size() * 8, '\0');
    for (std::size_t i = 0; i < built.entries.size(); ++i) {
        put32(body, i * 8, built.entries[i].first);
        put32(body, i * 8 + 4, static_cast<std::uint32_t>(built.entries[i].second));
    }
    std::string header(HEADER_BYTES, '\0');
    std::memcpy(header.data(), MAGIC, 8);
    put32(header, 8, FORMAT_VERSION);
    put32(header, 12, static_cast<std::uint32_t>(built.entries.size()));
    std::memcpy(header.data() + 16, built.venue.data(), built.venue.size());
    put64(header, 24, built.source_rows);
    put64(header, 32, built_at_ns ? *built_at_ns : static_cast<std::uint64_t>(util::unix_now_ns()));
    put32(header, 40, built.entries.front().first);
    put32(header, 44, built.entries.back().first);
    put64(header, 48, fnv1a(body));
    return header + body;
}

Info verify(const std::string& blob, const std::string& venue) {
    if (blob.size() < HEADER_BYTES || blob.compare(0, 8, MAGIC) != 0) throw ValueError("magic is wrong, expected MDFVTOK1");
    std::uint32_t version = get32(blob, 8), count = get32(blob, 12);
    if (version != FORMAT_VERSION) throw ValueError("format version " + std::to_string(version) + ", expected 1");
    std::string stamped = blob.substr(16, 8);
    stamped = stamped.substr(0, stamped.find('\0'));
    if (!venue.empty() && stamped != util::upper(venue))
        throw ValueError("venue " + util::quoted(stamped) + ", expected " + util::quoted(util::upper(venue)));
    if (blob.substr(56, 8) != std::string(8, '\0')) throw ValueError("reserved bytes 56..64 are not zero");
    std::size_t expected = HEADER_BYTES + static_cast<std::size_t>(count) * 8;
    if (blob.size() != expected)
        throw ValueError("file is " + std::to_string(blob.size()) + " bytes, expected exactly " + std::to_string(expected) +
                         " (header + " + std::to_string(count) + " entries) -- trailing bytes are a load failure");
    if (count == 0) throw ValueError("entry_count is 0");
    std::uint32_t lo = get32(blob, 40), hi = get32(blob, 44);
    if (fnv1a(blob.substr(HEADER_BYTES)) != get64(blob, 48)) throw ValueError("FNV-1a mismatch over the entry array");
    std::optional<std::uint32_t> previous;
    for (std::uint32_t i = 0; i < count; ++i) {
        std::uint32_t id = get32(blob, HEADER_BYTES + i * 8);
        auto token = static_cast<std::int32_t>(get32(blob, HEADER_BYTES + i * 8 + 4));
        if (id == 0) throw ValueError("entry " + std::to_string(i) + ": instrument_id 0 is reserved");
        if (token < 1) throw ValueError("entry " + std::to_string(i) + ": token " + std::to_string(token) + " outside 1..2147483647");
        if (previous && id <= *previous)
            throw ValueError("entry " + std::to_string(i) + ": instrument_id " + std::to_string(id) +
                             " not strictly greater than " + std::to_string(*previous));
        if (i == 0 && id != lo) throw ValueError("header min " + std::to_string(lo) + " disagrees with first entry " + std::to_string(id));
        previous = id;
    }
    if (*previous != hi) throw ValueError("header max " + std::to_string(hi) + " disagrees with last entry " + std::to_string(*previous));
    return {stamped, count, lo, hi, blob.size()};
}

std::filesystem::path tokenmap_dir(const std::string& date_dir) { return paths::plugin_dir(date_dir) / "tokenmap"; }

std::vector<std::filesystem::path> emit(const std::string& date_dir, const std::vector<std::string>& markets,
                                        const std::filesystem::path& out_dir) {
    log::out("  Building MDF token maps...");
    auto normalized = exporter::normalized_files(date_dir);
    if (normalized.empty()) {
        log::out("    No normalized files found");
        return {};
    }
    std::set<std::string> wanted;
    for (const auto& m : markets) wanted.insert(util::upper(m));
    std::vector<std::filesystem::path> written;
    for (const auto& src : normalized) {
        std::string venue = exporter::exchange_of(src);
        if (!wanted.count(util::upper(venue))) continue;
        const auto* cfg = config::venue(venue);
        if (cfg == nullptr || !cfg->enabled) {
            log::out("    Skipping " + venue + ": enabled = 0");
            continue;
        }
        if (cfg->feed != "databento") {
            log::out("    Skipping " + venue + ": feed = " + cfg->feed + ", no DBN instrument_id");
            continue;
        }
        std::vector<std::map<std::string, std::string>> rows;
        parquet::iter_rows(src, parquet::CHUNK_ROWS, [&](const std::vector<std::string>& columns, const std::vector<parquet::Row>& batch) {
            for (const auto& raw : batch) {
                std::map<std::string, std::string> r;
                for (std::size_t c = 0; c < columns.size(); ++c) r[columns[c]] = raw[c];
                rows.push_back(std::move(r));
            }
        }, {"def_raw_instrument_id", "scriptToken", "counterTokenV2"});
        Built built;
        std::string blob;
        Info info;
        try {
            built = build(venue, rows);
            blob = encode(built);
            info = verify(blob, venue);
        } catch (const ValueError& exc) {
            log::out("    CRITICAL [" + venue + "] token map not written -- " + exc.what());
            continue;
        }
        auto path = out_dir / filename(venue);
        std::filesystem::create_directories(out_dir);
        auto temp = util::staging_path(path);
        util::write_file_fsync(temp, blob);
        util::replace_file(temp, path);
        written.push_back(path);
        log::out("    " + venue + ": " + util::commas(static_cast<std::int64_t>(built.source_rows)) + " source row(s) -> " +
                 util::commas(info.entries) + " entries via " + built.key + ", ids " + util::commas(info.min_id) + ".." +
                 util::commas(info.max_id) + ", " + util::commas(static_cast<std::int64_t>(info.bytes)) + " bytes");
        log::out("      skipped: " + built.skips.describe());
        log::out("      wrote " + path.string());
    }
    if (!written.empty()) {
        log::out("    " + std::to_string(written.size()) + " map(s) in " + out_dir.string());
        log::out("    deliver into MDF's config/cpp-vendor/ -- the ini references the bare filename, so it must resolve "
                 "there with no path");
    }
    return written;
}

}  // namespace tokenmap
}  // namespace premarket::plugin
