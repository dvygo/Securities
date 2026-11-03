#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "premarket/normalize/row.hpp"
#include "premarket/runner.hpp"

namespace premarket::baskets {

using ContractRow = std::vector<std::string>;

std::vector<std::string> load_basket_scripts(const std::filesystem::path& file);
std::string parse_eq_root(const std::string& script);
std::string parse_fut_root(const std::string& script);

class Index {
public:
    using Row = row::Row;
    Index(std::string mic, const std::filesystem::path& normalized);

    bool present() const { return present_; }
    const Row* by_script(const std::string& script) const;
    std::vector<const Row*> live_futures(const std::string& root, std::int64_t as_of_ns) const;
    std::vector<const Row*> live_options(const std::string& root, std::int64_t as_of_ns) const;
    ContractRow to_contract_row(const Row& r, const std::string& as_of) const;
    static bool is_future(const Row& r);
    static bool is_option(const Row& r);

private:
    std::string mic_;
    bool present_ = false;
    std::vector<int> col_;
    std::vector<Row> rows_;
    std::map<std::string, std::size_t> by_script_;
    std::map<std::string, std::vector<std::size_t>> futures_, options_;
    std::vector<const Row*> live(const std::map<std::string, std::vector<std::size_t>>& table, const std::string& root,
                                 std::int64_t as_of_ns) const;
};

std::vector<ContractRow> refresh_basket(const std::string& name, const std::string& as_of);
void refresh_all(const std::string& as_of, const std::string& venue, bool dry_run);
void run(runner::Opts& opts);

}  // namespace premarket::baskets
