#include "premarket/runner.hpp"

#include <algorithm>

#include "premarket/args.hpp"
#include "premarket/log.hpp"
#include "premarket/util.hpp"

namespace premarket::runner {

bool venue_selected(const Opts& opts, const std::string& mic) { return util::upper(mic) == opts.venue; }

std::vector<Step> select_steps(const std::string& venue, const std::vector<std::string>& only,
                               const std::vector<Step>& steps) {
    if (only.empty()) return steps;
    std::vector<std::string> known;
    for (const auto& s : steps) known.push_back(s.name);
    for (const auto& item : only)
        if (std::find(known.begin(), known.end(), item) == known.end())
            throw args::UsageError{"--only " + item + ": not a step of " + venue + " (its steps: " + util::join(known, ", ") + ")"};
    std::vector<Step> out;
    for (const auto& s : steps)
        if (std::find(only.begin(), only.end(), s.name) != only.end()) out.push_back(s);
    return out;
}

int run(const std::vector<Step>& steps, Opts& opts) {
    if (steps.empty()) {
        log::err("error: --only matched no steps");
        return 1;
    }
    for (const auto& step : steps) {
        log::err(">>> " + step.name);
        try {
            step.run(opts);
        } catch (const std::exception& e) {
            log::err("error: " + step.name + ": " + e.what());
            return 1;
        }
    }
    return 0;
}

}  // namespace premarket::runner
