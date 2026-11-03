#pragma once

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace premarket::numbering_session {
class Session;
}

namespace premarket::runner {

struct Opts {
    std::string as_of;
    std::string date_dir;
    std::string venue;
    bool dry_run = false;
    std::optional<std::string> basket;
    std::optional<std::string> csv_dir;
    std::optional<std::string> symbols_file;
    numbering_session::Session* numbering = nullptr;
};

struct Step {
    std::string name;
    std::function<void(Opts&)> run;
};

bool venue_selected(const Opts& opts, const std::string& mic);
std::vector<Step> select_steps(const std::string& venue, const std::vector<std::string>& only,
                               const std::vector<Step>& steps);
int run(const std::vector<Step>& steps, Opts& opts);

std::vector<Step> build_normalizer_steps(const std::string& venue, const std::vector<std::string>& only);
std::vector<Step> build_download_steps(const std::string& venue);

}  // namespace premarket::runner
