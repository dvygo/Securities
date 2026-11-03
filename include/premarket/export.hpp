#pragma once

#include <filesystem>
#include <functional>
#include <string>
#include <vector>

#include "premarket/runner.hpp"

namespace premarket::exporter {

inline constexpr std::size_t CONTRACT_BATCH_ROWS = 50000;

struct BasketRow {
    std::string date, basket, script;
    std::vector<std::string> nexus;
};

std::vector<std::filesystem::path> normalized_files(const std::string& date_dir);
std::string exchange_of(const std::filesystem::path& normalized_file);
void iter_contract_rows(const std::string& date_dir, std::size_t batch_rows,
                        const std::function<void(const std::vector<std::vector<std::string>>&)>& fn);
std::vector<BasketRow> aggregate_basket_rows(const std::string& date_dir);
void write_aggregate_csvs(const std::string& date_dir, const std::filesystem::path& output_dir);
void run(runner::Opts& opts);

}  // namespace premarket::exporter
