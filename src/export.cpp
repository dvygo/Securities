#include "premarket/export.hpp"

#include <set>

#include "premarket/csv.hpp"
#include "premarket/log.hpp"
#include "premarket/parquet_io.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::exporter {

std::vector<std::filesystem::path> normalized_files(const std::string& date_dir) {
    std::vector<std::filesystem::path> out;
    auto dir = paths::normalized_dir(date_dir);
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return out;
    for (const auto& p : util::list_dir_sorted(dir)) {
        std::string name = p.filename().string();
        if (util::ends_with(name, parquet::SUFFIX) && !util::starts_with(name, "NSE-")) out.push_back(p);
    }
    return out;
}

std::string exchange_of(const std::filesystem::path& normalized_file) {
    std::string name = normalized_file.filename().string();
    return name.substr(0, name.find('-'));
}

void iter_contract_rows(const std::string& date_dir, std::size_t batch_rows,
                        const std::function<void(const std::vector<std::vector<std::string>>&)>& fn) {
    std::set<std::pair<std::string, std::string>> seen;
    for (const auto& path : normalized_files(date_dir)) {
        std::string exchange = exchange_of(path);
        std::vector<std::vector<std::string>> batch;
        try {
            parquet::iter_rows(path, parquet::CHUNK_ROWS, [&](const std::vector<std::string>& columns,
                                                              const std::vector<parquet::Row>& rows) {
                std::vector<int> index;
                for (const auto& c : paths::NORMALIZED_COLUMNS) {
                    auto it = std::find(columns.begin(), columns.end(), c);
                    index.push_back(it == columns.end() ? -1 : static_cast<int>(it - columns.begin()));
                }
                int i_token = index[10], i_script = index[9];
                for (const auto& row : rows) {
                    std::string token = i_token >= 0 ? row[static_cast<std::size_t>(i_token)] : "";
                    if (token.empty()) continue;
                    std::string script = i_script >= 0 ? row[static_cast<std::size_t>(i_script)] : "";
                    if (!seen.insert({token, script}).second) continue;
                    std::vector<std::string> out = {date_dir, exchange};
                    for (int i : index) out.push_back(i >= 0 ? row[static_cast<std::size_t>(i)] : "");
                    batch.push_back(std::move(out));
                    if (batch.size() >= batch_rows) {
                        fn(batch);
                        batch.clear();
                    }
                }
            });
        } catch (const std::exception& e) {
            log::out("  Error reading " + path.string() + ": " + e.what());
        }
        if (!batch.empty()) fn(batch);
    }
}

std::vector<BasketRow> aggregate_basket_rows(const std::string& date_dir) {
    std::vector<BasketRow> rows;
    auto dir = paths::contracts_day_dir(date_dir);
    std::error_code ec;
    if (!std::filesystem::is_directory(dir, ec)) return rows;
    for (const auto& path : util::list_dir_sorted(dir)) {
        std::string name = path.filename().string();
        if (!util::ends_with(name, parquet::SUFFIX)) continue;
        std::string basket = name.substr(0, name.size() - std::string(parquet::SUFFIX).size());
        try {
            auto table = parquet::read_table(path);
            int i_script = table.index("script"), i_symbol = table.index("symbol");
            for (const auto& row : table.rows) {
                std::string script = i_script >= 0 ? row[static_cast<std::size_t>(i_script)].value_or("") : "";
                if (script.empty() && i_symbol >= 0) script = row[static_cast<std::size_t>(i_symbol)].value_or("");
                if (script.empty()) continue;
                BasketRow b{date_dir, basket, script, {}};
                for (const auto& col : paths::NEXUS_BASKET_COLUMNS) {
                    if (col == "script") continue;
                    int i = table.index(col);
                    b.nexus.push_back(i >= 0 ? row[static_cast<std::size_t>(i)].value_or("") : "");
                }
                rows.push_back(std::move(b));
            }
        } catch (const std::exception& e) {
            log::out("  Error reading " + path.string() + ": " + e.what());
        }
    }
    return rows;
}

void write_aggregate_csvs(const std::string& date_dir, const std::filesystem::path& output_dir) {
    std::filesystem::create_directories(output_dir);
    log::out("  Exporting contracts.csv...");
    std::string contracts = "\xEF\xBB\xBF" + csv::format_line(paths::CONTRACT_COLUMNS) + "\n";
    std::size_t n = 0;
    iter_contract_rows(date_dir, CONTRACT_BATCH_ROWS, [&](const auto& batch) {
        for (const auto& row : batch) contracts += csv::format_line(row) + "\n";
        n += batch.size();
    });
    if (n) {
        auto out = output_dir / "contracts.csv";
        util::write_file(out, contracts);
        log::out("    Wrote " + std::to_string(n) + " rows to " + out.string());
    }
    log::out("  Exporting baskets.csv...");
    auto baskets = aggregate_basket_rows(date_dir);
    if (!baskets.empty()) {
        std::vector<std::string> header = {"date", "basket", "script"};
        for (const auto& c : paths::NEXUS_BASKET_COLUMNS)
            if (c != "script") header.push_back(c);
        std::string text = "\xEF\xBB\xBF" + csv::format_line(header) + "\n";
        for (const auto& b : baskets) {
            std::vector<std::string> row = {b.date, b.basket, b.script};
            row.insert(row.end(), b.nexus.begin(), b.nexus.end());
            text += csv::format_line(row) + "\n";
        }
        auto out = output_dir / "baskets.csv";
        util::write_file(out, text);
        log::out("    Wrote " + std::to_string(baskets.size()) + " rows to " + out.string());
    }
}

void run(runner::Opts& opts) {
    if (opts.dry_run) {
        log::out("DRY RUN: Would export CSVs");
        return;
    }
    log::out("  Exporting CSVs...");
    if (!opts.csv_dir) {
        log::out("    No --csv directory specified, skipping CSV export");
        return;
    }
    write_aggregate_csvs(opts.date_dir, *opts.csv_dir);
}

}  // namespace premarket::exporter
