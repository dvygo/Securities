#pragma once

#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace premarket::numbering_session {
class Session;
}

namespace premarket::runner {

struct Opts {
    std::string as_of;
    std::string date_dir;
    bool dry_run = false;
    std::optional<std::string> input_path;
    std::optional<std::string> basket;
    std::optional<std::string> csv_dir;
    bool include_csv_header = false;
    bool all_symbols = false;
    std::optional<std::string> symbols_file;
    std::optional<std::string> stype_in;
    std::optional<std::string> live_start;
    std::optional<std::string> hist_range;
    std::vector<std::string> venues;
    numbering_session::Session* numbering = nullptr;
};

struct Step {
    std::string name;
    std::function<void(Opts&)> run;
};

bool venue_selected(const Opts& opts, const std::string& mic);
std::pair<std::string, std::string> parse_hist_range(const std::string& range);
std::vector<Step> expand_only(const std::vector<std::string>& only, const std::vector<Step>& all_steps);
int run(const std::vector<Step>& steps, Opts& opts);

std::vector<Step> build_normalizer_steps(const std::vector<std::string>& only);
std::vector<Step> build_download_steps(const std::string& venue, const std::optional<std::string>& mode);

}  // namespace premarket::runner
