#include "premarket/args.hpp"
#include "premarket/baskets.hpp"
#include "premarket/commands.hpp"
#include "premarket/export.hpp"
#include "premarket/load.hpp"
#include "premarket/normalize/migrate.hpp"
#include "premarket/qa.hpp"
#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/databento_norm.hpp"
#include "premarket/normalize/fields.hpp"
#include "premarket/normalize/mcx_contract.hpp"
#include "premarket/normalize/nse_contract.hpp"
#include "premarket/sources/databento.hpp"
#include "premarket/normalize/state.hpp"
#include "premarket/paths.hpp"
#include "premarket/runner.hpp"
#include "premarket/sources/fyers.hpp"
#include "premarket/util.hpp"

#include <set>

namespace premarket {

namespace runner {

std::vector<Step> build_normalizer_steps(const std::string& venue, const std::vector<std::string>& only) {
    const auto* v = config::venue(venue);
    if (v == nullptr) throw ValueError("no [venue." + venue + "] in " + config::get().file.string());
    std::vector<Step> steps;
    if (v->feed == "fyers") steps.push_back({"normalize-fyers", fields::run});
    else if (v->feed == "nse") steps.push_back({"normalize-nse-contract", nse_contract::run});
    else if (v->feed == "mcx") steps.push_back({"normalize-mcx-contract", mcx_contract::run});
    else steps.push_back({"normalize-databento", databento_norm::run});
    bool has_baskets = !paths::basket_names_of(venue).empty();
    if (has_baskets && config::get().baskets_enabled) steps.push_back({"baskets", baskets::run});
    steps.push_back({"csv-export", exporter::run});
    if (has_baskets && !config::get().baskets_enabled && std::find(only.begin(), only.end(), "baskets") != only.end())
        throw args::UsageError{"--only baskets: [baskets] enabled = 0 in " + config::get().file.string()};
    return select_steps(venue, only, steps);
}

std::vector<Step> build_download_steps(const std::string& venue) {
    const auto* v = config::venue(venue);
    if (v == nullptr) throw ValueError("no [venue." + venue + "] in " + config::get().file.string());
    std::string name = "download-" + util::lower(venue);
    if (v->feed == "databento") return {{name, [](Opts& opts) { databento_src::download(opts, opts.venue); }}};
    if (v->feed == "fyers") return {{name, fyers::download}};
    std::string where = v->feed == "nse" ? "<data_dir>/YYYYMMDD/XNSE/" + std::string(nse_contract::DROP_DIR) + "/"
                                         : "<data_dir>/YYYYMMDD/" + std::string(mcx_contract::DROP_DIR) + "/";
    throw ValueError(venue + " is on the " + v->feed + " feed: its contract file is placed in " + where +
                     " by the BOD process, not downloaded");
}

}  // namespace runner

namespace commands {

int load(const std::string& date_dir, const std::string& venue, const std::vector<std::string>& sinks, bool dry_run) {
    return load::run(date_dir, venue, sinks, dry_run);
}
int check_tokens(const std::vector<std::string>& dates, const std::string& venue) {
    return qa::report(qa::token_checks(dates, venue), "check-tokens-" + venue);
}

int check_lineage(const std::vector<std::string>& dates, const std::string& venue) {
    return qa::report(qa::lineage_checks(dates, venue), "check-lineage-" + venue);
}

int check_state() { return qa::report(qa::state_checks(), "check-state"); }

int init_state(const std::string& reason, bool dry_run) {
    state::Lock lock("init-state");
    state::init_state(reason, dry_run, [](const std::string& s) { log::out(s); });
    return 0;
}

int migrate_state(const std::string& reason, bool dry_run) { return migrate::migrate_state(reason, dry_run); }

int quality_gate(const std::vector<std::string>& dates, const std::string& venue, const std::string& parent_run) {
    std::set<std::string> days(dates.begin(), dates.end());
    auto known = state::venue_days(venue);
    for (const auto& date : dates) {
        std::optional<std::string> before, after;
        for (const auto& d : known) {
            if (d < date) before = d;
            if (d > date && !after) after = d;
        }
        if (before) days.insert(*before);
        if (after) days.insert(*after);
    }
    std::vector<std::string> sorted(days.begin(), days.end());
    log::err("Quality gate: check-tokens over " + util::join(sorted, ", ") + ", then check-state");
    int token_rc = qa::report(qa::token_checks(sorted, venue), "check-tokens-" + venue);
    int state_rc = qa::report(qa::state_checks(), "check-state");
    bool ok = token_rc == 0 && state_rc == 0;
    jsonio::Json ds = jsonio::Json::array();
    for (const auto& d : sorted) ds.push_back(d);
    state::append_event(state::event(ok ? "COMPLETE" : "FAIL", state::new_run_id(dates.front(), "gate"), "quality-gate",
                                     {{"dates", ds}, {"venue", venue}, {"check_tokens", token_rc}, {"check_state", state_rc}},
                                     jsonio::Json::array(), jsonio::Json::array(), parent_run));
    if (!ok)
        log::err("error: the quality gate failed -- see the FAIL lines above. The filled days are on disk but must not be "
                 "used until this is resolved.");
    return ok ? 0 : 1;
}

}  // namespace commands
}  // namespace premarket
