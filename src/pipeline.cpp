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
#include "premarket/runner.hpp"
#include "premarket/sources/fyers.hpp"
#include "premarket/util.hpp"

#include <set>

namespace premarket {

namespace runner {

std::vector<Step> build_normalizer_steps(const std::vector<std::string>& only) {
    bool baskets_enabled = config::get().baskets_enabled;
    std::vector<Step> all = {
        {"normalize-fyers", fields::run},
        {"normalize-nse-contract", nse_contract::run},
        {"normalize-mcx-contract", mcx_contract::run},
        {"normalize-databento", databento_norm::run},
    };
    if (baskets_enabled) all.push_back({"baskets", baskets::run});
    all.push_back({"csv-export", exporter::run});
    auto steps = expand_only(only, all);
    if (!baskets_enabled && std::find(only.begin(), only.end(), "baskets") != only.end())
        log::err("[baskets] enabled = 0 in " + config::get().file.string() + ": skipping baskets");
    return steps;
}

std::vector<Step> build_download_steps(const std::string& venue, const std::optional<std::string>&) {
    if (venue == "india") return {{"download-india", fyers::download}};
    return {{"download-" + venue, [venue](Opts& opts) { databento_src::download(opts, venue); }}};
}

}  // namespace runner

namespace commands {

int load(const std::string& date_dir, const std::vector<std::string>& sinks, bool dry_run) {
    return load::run(date_dir, sinks, dry_run);
}
int check_tokens(const std::vector<std::string>& dates, const std::vector<std::string>& venues) {
    return qa::report(qa::token_checks(dates, venues), "check-tokens");
}

int check_lineage(const std::vector<std::string>& dates, const std::vector<std::string>& venues) {
    return qa::report(qa::lineage_checks(dates, venues), "check-lineage");
}

int check_state() { return qa::report(qa::state_checks(), "check-state"); }

int init_state(const std::string& reason, bool dry_run) {
    state::Lock lock("init-state");
    state::init_state(reason, dry_run, [](const std::string& s) { log::out(s); });
    return 0;
}

int migrate_state(const std::string& reason, bool dry_run) { return migrate::migrate_state(reason, dry_run); }

int quality_gate(const std::vector<std::string>& dates, const std::vector<std::string>& venues, const std::string& parent_run) {
    std::set<std::string> days(dates.begin(), dates.end());
    for (const auto& mic : venues) {
        auto known = state::venue_days(mic);
        for (const auto& date : dates) {
            std::optional<std::string> before, after;
            for (const auto& d : known) {
                if (d < date) before = d;
                if (d > date && !after) after = d;
            }
            if (before) days.insert(*before);
            if (after) days.insert(*after);
        }
    }
    std::vector<std::string> sorted(days.begin(), days.end());
    log::err("Quality gate: check-tokens over " + util::join(sorted, ", ") + ", then check-state");
    int token_rc = qa::report(qa::token_checks(sorted, venues), "check-tokens");
    int state_rc = qa::report(qa::state_checks(), "check-state");
    bool ok = token_rc == 0 && state_rc == 0;
    jsonio::Json ds = jsonio::Json::array(), vs = jsonio::Json::array();
    for (const auto& d : sorted) ds.push_back(d);
    for (const auto& v : venues) vs.push_back(v);
    state::append_event(state::event(ok ? "COMPLETE" : "FAIL", state::new_run_id(dates.front(), "gate"), "quality-gate",
                                     {{"dates", ds}, {"venues", vs}, {"check_tokens", token_rc}, {"check_state", state_rc}},
                                     jsonio::Json::array(), jsonio::Json::array(), parent_run));
    if (!ok)
        log::err("error: the quality gate failed -- see the FAIL lines above. The filled days are on disk but must not be "
                 "used until this is resolved.");
    return ok ? 0 : 1;
}

}  // namespace commands
}  // namespace premarket
