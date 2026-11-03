#include "premarket/baskets.hpp"

#include <algorithm>
#include <set>

#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::baskets {

namespace {

const std::set<std::string> kMonthCodes = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};

std::int64_t int_field(const Index::Row& row, std::size_t col) {
    std::string raw = util::strip(row[col]);
    if (raw.empty()) return 0;
    auto f = util::parse_int_truncating(raw);
    return f ? *f : 0;
}

}  // namespace

std::vector<std::string> load_basket_scripts(const std::filesystem::path& file) {
    std::vector<std::string> out;
    for (const auto& line : util::splitlines(util::read_text_utf8_sig(file))) {
        std::string s = util::strip(line);
        if (s.empty() || s[0] == '#') continue;
        out.push_back(s);
    }
    return out;
}

std::string parse_eq_root(const std::string& script_in) {
    std::string s = util::strip(script_in);
    auto colon = s.find(':');
    if (colon == std::string::npos || colon == 0) return "";
    std::string rest = s.substr(colon + 1);
    if (rest.size() < 4 || !util::ends_with(rest, "-EQ")) return "";
    return util::upper(rest.substr(0, rest.size() - 3));
}

std::string parse_fut_root(const std::string& script_in) {
    std::string s = util::strip(script_in);
    auto colon = s.find(':');
    if (colon == std::string::npos || colon == 0) return "";
    std::string rest = s.substr(colon + 1);
    if (rest.size() < 9 || !util::ends_with(rest, "FUT")) return "";
    std::string tail = rest.substr(rest.size() - 8);
    if (!util::is_digits(tail.substr(0, 2)) || !kMonthCodes.count(tail.substr(2, 3))) return "";
    return util::upper(rest.substr(0, rest.size() - 8));
}

Index::Index(std::string mic, const std::filesystem::path& normalized) : mic_(std::move(mic)) {
    std::error_code ec;
    present_ = std::filesystem::exists(normalized, ec);
    if (!present_) {
        log::out("    " + mic_ + ": no normalized data at " + normalized.filename().string() +
                 " -- baskets on this venue will be empty");
        return;
    }
    auto names = parquet::column_names(normalized);
    for (const auto& c : paths::NORMALIZED_COLUMNS) {
        auto it = std::find(names.begin(), names.end(), c);
        col_.push_back(it == names.end() ? -1 : static_cast<int>(it - names.begin()));
    }
    parquet::iter_rows(normalized, parquet::CHUNK_ROWS, [&](const std::vector<std::string>&, const std::vector<parquet::Row>& rows) {
        for (const auto& raw : rows) {
            Row r(paths::NORMALIZED_COLUMNS.size());
            for (std::size_t c = 0; c < col_.size(); ++c)
                if (col_[c] >= 0) r[c] = raw[static_cast<std::size_t>(col_[c])];
            rows_.push_back(std::move(r));
        }
    });
    for (std::size_t i = 0; i < rows_.size(); ++i) {
        const auto& r = rows_[i];
        std::string script = util::strip(r[row::script]);
        if (!script.empty()) by_script_[script] = i;
        std::string root = util::upper(util::strip(r[row::underlying_root]));
        if (root.empty()) continue;
        if (is_future(r)) futures_[root].push_back(i);
        else if (is_option(r)) options_[root].push_back(i);
    }
}

bool Index::is_future(const Row& r) {
    if (util::starts_with(util::upper(util::strip(r[row::scriptInstrumentType])), "FUT")) return true;
    return util::ends_with(util::upper(util::strip(r[row::script])), "FUT");
}

bool Index::is_option(const Row& r) {
    if (!util::strip(r[row::optionType]).empty()) return true;
    if (util::starts_with(util::upper(util::strip(r[row::scriptInstrumentType])), "OPT")) return true;
    std::string s = util::upper(util::strip(r[row::script]));
    return util::ends_with(s, "CE") || util::ends_with(s, "PE");
}

const Index::Row* Index::by_script(const std::string& script) const {
    auto it = by_script_.find(script);
    return it == by_script_.end() ? nullptr : &rows_[it->second];
}

std::vector<const Index::Row*> Index::live(const std::map<std::string, std::vector<std::size_t>>& table,
                                           const std::string& root, std::int64_t as_of_ns) const {
    std::vector<const Row*> out;
    auto it = table.find(util::upper(root));
    if (it == table.end()) return out;
    for (auto i : it->second)
        if (int_field(rows_[i], row::expiration) >= as_of_ns) out.push_back(&rows_[i]);
    return out;
}

std::vector<const Index::Row*> Index::live_futures(const std::string& root, std::int64_t as_of_ns) const {
    return live(futures_, root, as_of_ns);
}

std::vector<const Index::Row*> Index::live_options(const std::string& root, std::int64_t as_of_ns) const {
    return live(options_, root, as_of_ns);
}

ContractRow Index::to_contract_row(const Row& r, const std::string& as_of) const {
    ContractRow out = {as_of, mic_};
    out.insert(out.end(), r.begin(), r.end());
    return out;
}

namespace {

using Cache = std::map<std::string, std::unique_ptr<Index>>;

std::string normalized_output(const std::string& mic) {
    const auto& outputs = paths::feed_outputs();
    auto by_feed = outputs.find(mic);
    if (by_feed == outputs.end()) {
        std::vector<std::string> known;
        for (const auto& [m, f] : outputs) known.push_back(m);
        throw ValueError(mic + ": no normalized output is defined for this MIC (known: " + util::join(known, ", ") + ")");
    }
    std::string feed = config::feed_for(mic);
    auto it = by_feed->second.find(feed);
    if (it == by_feed->second.end()) {
        std::vector<std::string> feeds;
        for (const auto& [f, o] : by_feed->second) feeds.push_back(f);
        throw ValueError(mic + ": " + config::get().file.string() + " says feed = " + util::quoted(feed) +
                         ", but no normalize step writes " + mic + " from that feed (this MIC is served by " +
                         util::join(feeds, ", ") + "). Fix [venue." + mic + "] feed =.");
    }
    return it->second;
}

Index& sym_index(const std::string& as_of, const std::string& mic, Cache& cache) {
    auto it = cache.find(mic);
    if (it == cache.end())
        it = cache.emplace(mic, std::make_unique<Index>(mic, paths::normalized_dir(as_of) / normalized_output(mic))).first;
    return *it->second;
}

const Index::Row* nearest(const std::vector<const Index::Row*>& rows) {
    const Index::Row* best = nullptr;
    for (const auto* r : rows)
        if (best == nullptr || int_field(*r, row::expiration) < int_field(*best, row::expiration)) best = r;
    return best;
}

std::vector<const Index::Row*> by_expiry(std::vector<const Index::Row*> rows) {
    std::stable_sort(rows.begin(), rows.end(), [](const Index::Row* a, const Index::Row* b) {
        return int_field(*a, row::expiration) < int_field(*b, row::expiration);
    });
    return rows;
}

std::vector<ContractRow> resolve_by_script(const std::string& name, const std::filesystem::path& file, const Index& idx,
                                           const std::string& as_of) {
    auto scripts = load_basket_scripts(file);
    std::vector<ContractRow> out;
    std::size_t missing = 0;
    for (const auto& s : scripts) {
        const auto* r = idx.by_script(s);
        if (r == nullptr) {
            ++missing;
            continue;
        }
        out.push_back(idx.to_contract_row(*r, as_of));
    }
    if (missing)
        log::out("    " + name + ": " + std::to_string(missing) + "/" + std::to_string(scripts.size()) +
                 " constituents not found in today's contracts");
    return out;
}

std::vector<ContractRow> resolve_futures(const std::string& name, const std::filesystem::path& file, const Index& idx,
                                         const std::string& as_of, bool near_only, bool equity_roots) {
    auto scripts = load_basket_scripts(file);
    std::int64_t as_of_ns = util::day_start_ns(as_of);
    std::vector<ContractRow> out;
    std::size_t no_fut = 0;
    std::set<std::string> seen;
    for (const auto& s : scripts) {
        std::string root = equity_roots ? parse_eq_root(s) : parse_fut_root(s);
        if (root.empty() || seen.count(root)) continue;
        seen.insert(root);
        auto live = idx.live_futures(root, as_of_ns);
        if (live.empty()) {
            ++no_fut;
            continue;
        }
        if (near_only) {
            if (const auto* picked = nearest(live)) out.push_back(idx.to_contract_row(*picked, as_of));
        } else {
            for (const auto* r : by_expiry(live)) out.push_back(idx.to_contract_row(*r, as_of));
        }
    }
    if (no_fut) {
        if (equity_roots)
            log::out("    " + name + ": " + std::to_string(no_fut) + "/" + std::to_string(seen.size()) +
                     " underlyings have no live future today");
        else
            log::out("    " + name + ": " + std::to_string(no_fut) + "/" + std::to_string(scripts.size()) +
                     " roots rolled to no live future");
    }
    return out;
}

std::string expiry_month(const Index::Row& r) {
    std::int64_t ns = int_field(r, row::expiration);
    return ns <= 0 ? "" : util::utc_format_ns(ns, "%Y-%m");
}

std::vector<ContractRow> resolve_options_for_futures(const std::string& name, const std::filesystem::path& file,
                                                     const Index& idx, const std::string& as_of, bool near_only) {
    auto scripts = load_basket_scripts(file);
    std::int64_t as_of_ns = util::day_start_ns(as_of);
    std::vector<ContractRow> out;
    std::size_t no_opt = 0;
    std::set<std::string> seen;
    for (const auto& s : scripts) {
        std::string root = parse_fut_root(s);
        if (root.empty() || seen.count(root)) continue;
        seen.insert(root);
        auto live = idx.live_options(root, as_of_ns);
        if (near_only) {
            std::set<std::string> months;
            for (const auto* f : idx.live_futures(root, as_of_ns))
                if (auto m = expiry_month(*f); !m.empty()) months.insert(m);
            std::string nearest_month = months.empty() ? "" : *months.begin();
            std::vector<const Index::Row*> kept;
            if (!nearest_month.empty())
                for (const auto* r : live)
                    if (expiry_month(*r) == nearest_month) kept.push_back(r);
            live = kept;
        }
        if (live.empty()) {
            ++no_opt;
            continue;
        }
        std::stable_sort(live.begin(), live.end(), [](const Index::Row* a, const Index::Row* b) {
            auto ea = int_field(*a, row::expiration), eb = int_field(*b, row::expiration);
            return ea != eb ? ea < eb : (*a)[row::script] < (*b)[row::script];
        });
        for (const auto* r : live) out.push_back(idx.to_contract_row(*r, as_of));
    }
    if (no_opt)
        log::out("    " + name + ": " + std::to_string(no_opt) + "/" + std::to_string(seen.size()) +
                 " underlyings have no live option today");
    return out;
}

std::vector<ContractRow> refresh(const std::string& name, const std::string& as_of, Cache& cache) {
    auto dir = paths::baskets_dir();
    for (const auto& [basket, mic] : paths::EQUITY_BASKETS)
        if (basket == name) return resolve_by_script(name, dir / (name + ".csv"), sym_index(as_of, mic, cache), as_of);
    for (const auto& f : paths::INDEX_FUTURES_SOURCES)
        if (f.name == name)
            return resolve_futures(name, dir / (name + ".csv"), sym_index(as_of, f.source, cache), as_of, f.near_only, false);
    for (const auto& f : paths::EQUITY_FUTURES_SOURCES)
        if (f.name == name)
            return resolve_futures(name, dir / (f.source + ".csv"), sym_index(as_of, "XNSE", cache), as_of, f.near_only, true);
    for (const auto& o : paths::OPTION_BASKET_SOURCES)
        if (o.name == name)
            return resolve_options_for_futures(name, dir / (o.futures + ".csv"), sym_index(as_of, o.mic, cache), as_of,
                                               o.near_only);
    if (name == "ALL_INDEX_FUTURES") {
        std::vector<ContractRow> out;
        for (const auto& part : paths::ALL_INDEX_FUTURES_PARTS)
            for (const auto& f : paths::INDEX_FUTURES_SOURCES)
                if (f.name == part) {
                    auto rows = resolve_futures(part, dir / (part + ".csv"), sym_index(as_of, f.source, cache), as_of,
                                                f.near_only, false);
                    out.insert(out.end(), rows.begin(), rows.end());
                }
        return out;
    }
    throw ValueError("unknown basket " + util::quoted(name));
}

void write_basket(const std::string& name, const std::string& as_of, const std::vector<ContractRow>& rows) {
    auto dir = paths::contracts_day_dir(as_of);
    std::filesystem::create_directories(dir);
    parquet::write_rows(dir / (name + parquet::SUFFIX), paths::CONTRACT_COLUMNS, rows);
}

}  // namespace

std::vector<ContractRow> refresh_basket(const std::string& name, const std::string& as_of) {
    Cache cache;
    return refresh(name, as_of, cache);
}

void refresh_all(const std::string& as_of, bool dry_run) {
    std::filesystem::create_directories(paths::contracts_day_dir(as_of));
    Cache cache;
    std::vector<std::string> failed, empty;
    for (const auto& name : paths::basket_names()) {
        if (dry_run) {
            log::out("    Would refresh basket " + name);
            continue;
        }
        try {
            auto rows = refresh(name, as_of, cache);
            if (!rows.empty()) {
                write_basket(name, as_of, rows);
                log::out("    Wrote " + name + ": " + std::to_string(rows.size()) + " contracts");
            } else {
                empty.push_back(name);
            }
        } catch (const std::exception& e) {
            failed.push_back(name + ": " + e.what());
            log::out("    ERROR refreshing " + name + ": " + e.what());
        }
    }
    if (!empty.empty())
        log::out("    " + std::to_string(empty.size()) + " basket(s) resolved to no contracts: " + util::join(empty, ", "));
    if (!failed.empty()) {
        log::out("    " + std::to_string(failed.size()) + "/" + std::to_string(paths::basket_names().size()) +
                 " basket(s) FAILED:");
        for (const auto& m : failed) log::out("      " + m);
    }
}

void run(runner::Opts& opts) {
    if (opts.dry_run) {
        log::out("DRY RUN: Would refresh baskets");
        return;
    }
    log::out("  Refreshing baskets...");
    std::error_code ec;
    if (!std::filesystem::exists(paths::normalized_dir(opts.date_dir), ec)) {
        log::out("    No normalized data directory");
        return;
    }
    if (opts.basket) {
        try {
            auto file = paths::baskets_dir() / (*opts.basket + ".csv");
            if (!std::filesystem::exists(file, ec)) {
                log::out("    Basket " + *opts.basket + " not found at " + file.string());
                return;
            }
            auto rows = refresh_basket(*opts.basket, opts.date_dir);
            if (!rows.empty()) {
                write_basket(*opts.basket, opts.date_dir, rows);
                log::out("    Wrote " + *opts.basket + ": " + std::to_string(rows.size()) + " rows");
            }
        } catch (const std::exception& e) {
            log::out("    Error refreshing " + *opts.basket + ": " + e.what());
        }
        return;
    }
    refresh_all(opts.date_dir, opts.dry_run);
}

}  // namespace premarket::baskets
