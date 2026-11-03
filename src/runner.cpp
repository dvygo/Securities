#include "premarket/runner.hpp"

#include <algorithm>
#include <map>
#include <set>

#include "premarket/args.hpp"
#include "premarket/log.hpp"
#include "premarket/util.hpp"

namespace premarket::runner {

bool venue_selected(const Opts& opts, const std::string& mic) {
    if (opts.venues.empty()) return true;
    std::string m = util::upper(mic);
    for (const auto& v : opts.venues)
        if (v == m) return true;
    return false;
}

std::vector<Step> expand_only(const std::vector<std::string>& only, const std::vector<Step>& all_steps) {
    if (only.empty()) return all_steps;
    for (const auto& o : only)
        if (o == "all") return all_steps;
    static const std::map<std::string, std::vector<std::string>> aliases = {
        {"fyers", {"normalize-fyers"}},
        {"databento", {"normalize-databento"}},
        {"normalize", {"normalize-fyers", "normalize-nse-contract", "normalize-mcx-contract", "normalize-databento", "baskets"}},
    };
    std::vector<std::string> known;
    for (const auto& s : all_steps) known.push_back(s.name);
    std::set<std::string> expanded;
    for (const auto& item : only) {
        if (auto it = aliases.find(item); it != aliases.end()) {
            expanded.insert(it->second.begin(), it->second.end());
        } else if (std::find(known.begin(), known.end(), item) != known.end()) {
            expanded.insert(item);
        } else {
            throw args::UsageError{"--only " + item + ": no such step (steps: " + util::join(known, ", ") +
                                   "; aliases: fyers, databento, normalize, all)"};
        }
    }
    std::vector<Step> out;
    for (const auto& s : all_steps)
        if (expanded.count(s.name)) out.push_back(s);
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
