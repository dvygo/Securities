#include "premarket/commands.hpp"
#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/fields.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/runner.hpp"
#include "premarket/sources/fyers.hpp"
#include "premarket/util.hpp"

namespace premarket {

namespace {

[[noreturn]] void not_ported(const std::string& what) { throw RuntimeError(what + " is not ported yet"); }

}  // namespace

namespace runner {

std::vector<Step> build_normalizer_steps(const std::vector<std::string>& only) {
    bool baskets_enabled = config::get().baskets_enabled;
    std::vector<Step> all = {
        {"normalize-fyers", fields::run},
    };
    (void)baskets_enabled;
    auto steps = expand_only(only, all);
    if (!baskets_enabled && std::find(only.begin(), only.end(), "baskets") != only.end())
        log::err("[baskets] enabled = 0 in " + config::get().file.string() + ": skipping baskets");
    return steps;
}

std::vector<Step> build_download_steps(const std::string& venue, const std::optional<std::string>&) {
    if (venue == "india") return {{"download-india", fyers::download}};
    not_ported("download-" + venue);
}

}  // namespace runner

namespace commands {

int load(const std::string&, const std::vector<std::string>&, bool) { not_ported("load"); }
int check_tokens(const std::vector<std::string>&, const std::vector<std::string>&) { not_ported("check-tokens"); }
int check_lineage(const std::vector<std::string>&, const std::vector<std::string>&) { not_ported("check-lineage"); }
int check_state() { not_ported("check-state"); }

int init_state(const std::string& reason, bool dry_run) {
    state::Lock lock("init-state");
    state::init_state(reason, dry_run, [](const std::string& s) { log::out(s); });
    return 0;
}

int migrate_state(const std::string&, bool) { not_ported("migrate-state"); }
int quality_gate(const std::vector<std::string>&, const std::vector<std::string>&, const std::string&) {
    not_ported("quality gate");
}

}  // namespace commands
}  // namespace premarket
