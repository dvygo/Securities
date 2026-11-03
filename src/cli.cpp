#include "premarket/cli.hpp"

#include <algorithm>
#include <iostream>
#include <set>

#include "premarket/args.hpp"
#include "premarket/commands.hpp"
#include "premarket/config.hpp"
#include "premarket/log.hpp"
#include "premarket/normalize/numbering_session.hpp"
#include "premarket/runlog.hpp"
#include "premarket/runner.hpp"
#include "premarket/util.hpp"

namespace premarket::cli {

namespace {

const char* kUsage = R"(usage: premarket --config=/abs/config.ini [--keys=/abs/keys.ini] <command> [options]

Pre-market symbology pipeline: download, normalize, load.

commands:
  india                   download Fyers' Indian symbol masters (XNSE, XBOM, XIMC)
  xnas | xcme | xcbo      download the venue's definitions: a live replay of today's session
  normalize               normalize a day and number it (counterTokenV2); --dates fills older days
  load                    deliver a normalized day through named sinks (<sinks_dir>/<name>.ini)
  check-tokens            validate counterTokenV2 against the normalized files and manifests
  check-lineage           trace each stage's output back to its input
  check-state             audit the numbering state against the days on disk and the run log
  init-state              create the numbering state, once per host
  migrate-state           move a 7.x numbering state and its manifests to v8, once

global options (accepted anywhere on the line, always full paths):
  --config=PATH           config.ini (required)
  --keys=PATH             keys.ini with the Databento keys (needed by xnas/xcme/xcbo)
  --help, --version

`premarket <command> --help` lists a command's options.
)";

struct Global {
    std::string config;
    std::string keys;
    bool help = false;
    bool version = false;
    std::vector<std::string> rest;
};

Global split_global(const std::vector<std::string>& argv) {
    Global g;
    for (std::size_t i = 0; i < argv.size(); ++i) {
        const std::string& a = argv[i];
        auto take = [&](const std::string& name, std::string& slot) {
            if (a == "--" + name) {
                if (i + 1 >= argv.size()) throw args::UsageError{"--" + name + " needs a value (PATH)"};
                slot = argv[++i];
                return true;
            }
            if (util::starts_with(a, "--" + name + "=")) {
                slot = a.substr(name.size() + 3);
                return true;
            }
            return false;
        };
        if (take("config", g.config) || take("keys", g.keys)) continue;
        if (a == "--version") {
            g.version = true;
            continue;
        }
        if (a == "--help" || a == "-h") {
            g.help = true;
            continue;
        }
        g.rest.push_back(a);
    }
    return g;
}

std::vector<args::Option> download_options(bool databento) {
    std::vector<args::Option> o = {
        {"date-dir", true, false, "YYYYMMDD", "day to download into (default: today in [run] timezone)"},
        {"dry-run", false, false, "", "say what would be downloaded; fetch nothing"},
    };
    if (databento)
        o.push_back({"symbols-file", true, false, "PATH", "absolute path of a symbol list; overrides the venue's `symbols`"});
    return o;
}

const std::vector<args::Option> kNormalize = {
    {"date-dir", true, false, "YYYYMMDD", "the live day (default: today in [run] timezone)"},
    {"dates", true, false, "D1,D2", "fill OLDER days, newest first; needs --venue and --reason"},
    {"reason", true, false, "TEXT", "why this run is happening; recorded in the run log and manifests"},
    {"dry-run", false, false, "", "live: write nothing; --dates: a real preview that writes nothing"},
    {"only", true, true, "STEP", "normalize-fyers, normalize-nse-contract, normalize-mcx-contract, normalize-databento, baskets, csv-export"},
    {"venue", true, true, "MIC", "restrict the per-venue steps to this MIC"},
    {"basket", true, false, "NAME", "refresh this basket only"},
    {"csv", true, false, "DIR", "also export the aggregated CSVs to this absolute directory"},
};

const std::vector<args::Option> kLoad = {
    {"date-dir", true, false, "YYYYMMDD", "day to push (default: today in [run] timezone)"},
    {"sink", true, true, "NAME", "a sink in <sinks_dir>/<NAME>.ini; default: every sink there"},
    {"dry-run", false, false, "", "validate every sink and say what it would push; connect to nothing"},
};

const std::vector<args::Option> kChecks = {
    {"dates", true, false, "D1,D2", "comma-separated YYYYMMDD (default: today in [run] timezone)"},
    {"venue", true, true, "MIC", "restrict to this MIC"},
};

const std::vector<args::Option> kReason = {
    {"reason", true, false, "TEXT", "why; recorded in the run log"},
    {"dry-run", false, false, "", "show what would be done and write nothing"},
};

std::string command_help(const std::string& cmd, const std::vector<args::Option>& options) {
    return "usage: premarket --config=PATH [--keys=PATH] " + cmd + " [options]\n\noptions:\n" + args::describe(options);
}

std::string date_or_today(const args::Parsed& p) {
    auto d = p.value("date-dir");
    std::string date = d ? *d : config::today();
    if (!util::valid_yyyymmdd(date)) throw SystemExit{1, "--date-dir: " + util::quoted(date) + " is not a YYYYMMDD date"};
    return date;
}

int run_download(const std::string& venue, const args::Parsed& p) {
    std::string date = date_or_today(p);
    runlog::Run log_run("premarket-" + venue, date);
    runner::Opts opts;
    opts.as_of = opts.date_dir = date;
    opts.dry_run = p.flag("dry-run");
    if (auto f = p.value("symbols-file")) {
        if (!std::filesystem::path(*f).is_absolute())
            throw SystemExit{1, "--symbols-file must be an absolute path, got `" + *f + "`"};
        opts.symbols_file = *f;
    }
    auto steps = runner::build_download_steps(venue, std::nullopt);
    return runner::run(steps, opts);
}

int numbered_run(const std::string& command, const std::vector<runner::Step>& steps, runner::Opts& opts, bool dry_run,
                 const std::string& reason) {
    numbering_session::Session session(command, numbering::LIVE, reason, dry_run, opts.date_dir);
    return numbering_session::with_session(session, [&]() {
        opts.numbering = &session;
        int rc = runner::run(steps, opts);
        if (rc != 0) session.fail("a step failed (exit " + std::to_string(rc) + ")");
        return rc;
    });
}

int run_normalize(const args::Parsed& p) {
    std::string date = date_or_today(p);
    auto only = p.all("only");
    runlog::Run log_run("normalizer", date);
    runner::Opts opts;
    opts.as_of = opts.date_dir = date;
    opts.dry_run = p.flag("dry-run");
    opts.basket = p.value("basket");
    opts.venues = venue_selection(p.all("venue"));
    if (auto c = p.value("csv")) {
        if (!std::filesystem::path(*c).is_absolute()) throw SystemExit{1, "--csv must be an absolute directory, got `" + *c + "`"};
        opts.csv_dir = *c;
    }
    auto steps = runner::build_normalizer_steps(only);
    return numbered_run("normalize", steps, opts, opts.dry_run, p.value("reason").value_or(""));
}

int run_fill(const args::Parsed& p) {
    auto dates = date_list(*p.value("dates"));
    auto venues = venue_selection(p.all("venue"));
    if (venues.empty())
        throw SystemExit{1, "--dates needs --venue: name every venue you are filling (e.g. --venue XCME --venue XNAS). "
                            "The scope of a manual backfill is explicit and recorded."};
    std::string reason = util::strip(p.value("reason").value_or(""));
    if (reason.empty())
        throw SystemExit{1, "--dates needs --reason \"...\": a backfill changes the record, and the run log keeps why."};
    auto steps = runner::build_normalizer_steps(p.all("only"));
    bool dry_run = p.flag("dry-run");
    runlog::Run log_run("normalizer-fill", dates.front());
    log::err("Filling " + util::join(dates, ", ") + " for " + util::join(venues, ", ") + " (newest first)" +
             (dry_run ? " -- PREVIEW, nothing is written" : ""));
    numbering_session::Session session("normalize", numbering::FILL, reason, dry_run,
                                       dates.back() + "-" + dates.front());
    int rc = numbering_session::with_session(session, [&]() {
        for (const auto& date : dates) {
            runner::Opts opts;
            opts.as_of = opts.date_dir = date;
            opts.dry_run = dry_run;
            opts.venues = venues;
            opts.numbering = &session;
            int step_rc = runner::run(steps, opts);
            std::set<std::string> done;
            std::vector<std::string> problems;
            for (const auto& o : session.outcomes) {
                if (o.date != date) continue;
                if (o.status == "done" || o.status == "previewed") done.insert(o.mic);
                if (std::find(venues.begin(), venues.end(), o.mic) != venues.end() &&
                    (o.status == "refused" || o.status == "no-input"))
                    problems.push_back(o.mic + ": " + o.status + " -- " + o.detail);
            }
            for (const auto& mic : venues) {
                bool mentioned = std::any_of(problems.begin(), problems.end(),
                                             [&](const std::string& pr) { return util::starts_with(pr, mic + ":"); });
                if (!done.count(mic) && !mentioned) problems.push_back(mic + ": nothing was numbered");
            }
            if (step_rc != 0 || !problems.empty()) {
                std::string why = problems.empty() ? "a step failed (exit " + std::to_string(step_rc) + ")"
                                                   : util::join(problems, "; ");
                log::err("error: " + date + " did not complete (" + why +
                         "). Stopping: older dates are filled from this one.");
                session.fail(date + ": " + why);
                return 1;
            }
        }
        return 0;
    });
    if (rc != 0) return rc;
    if (dry_run) {
        log::err("Preview complete; nothing was written.");
        return 0;
    }
    return commands::quality_gate(dates, venues, session.run_id);
}

int dispatch(const std::string& command, const std::vector<std::string>& rest) {
    auto parse = [&](const std::vector<args::Option>& options) { return args::parse(rest, options); };
    if (command == "india") return run_download("india", parse(download_options(false)));
    if (command == "xnas" || command == "xcme" || command == "xcbo") return run_download(command, parse(download_options(true)));
    if (command == "normalize") {
        auto p = parse(kNormalize);
        if (p.value("dates") && p.value("date-dir")) throw args::UsageError{"--dates and --date-dir are mutually exclusive"};
        return p.value("dates") ? run_fill(p) : run_normalize(p);
    }
    if (command == "load") {
        auto p = parse(kLoad);
        std::string date = date_or_today(p);
        runlog::Run log_run("load", date);
        return commands::load(date, p.all("sink"), p.flag("dry-run"));
    }
    if (command == "check-tokens" || command == "check-lineage") {
        auto p = parse(kChecks);
        auto dates = date_list(p.value("dates").value_or(config::today()));
        auto venues = venue_selection(p.all("venue"));
        return command == "check-tokens" ? commands::check_tokens(dates, venues) : commands::check_lineage(dates, venues);
    }
    if (command == "check-state") {
        parse({});
        return commands::check_state();
    }
    if (command == "init-state" || command == "migrate-state") {
        auto p = parse(kReason);
        auto reason = p.value("reason");
        if (!reason || util::strip(*reason).empty()) throw args::UsageError{command + " needs --reason \"...\""};
        return command == "init-state" ? commands::init_state(*reason, p.flag("dry-run"))
                                       : commands::migrate_state(*reason, p.flag("dry-run"));
    }
    throw args::UsageError{"unknown command: " + command};
}

std::string help_for(const std::string& command) {
    if (command == "india") return command_help(command, download_options(false));
    if (command == "xnas" || command == "xcme" || command == "xcbo") return command_help(command, download_options(true));
    if (command == "normalize") return command_help(command, kNormalize);
    if (command == "load") return command_help(command, kLoad);
    if (command == "check-tokens" || command == "check-lineage") return command_help(command, kChecks);
    if (command == "check-state") return command_help(command, {});
    if (command == "init-state" || command == "migrate-state") return command_help(command, kReason);
    return kUsage;
}

}  // namespace

std::vector<std::string> date_list(const std::string& raw) {
    std::vector<std::string> out;
    std::set<std::string> seen;
    for (const auto& part : util::split(raw, ",")) {
        std::string stamp = util::strip(part);
        if (stamp.empty()) continue;
        if (!util::valid_yyyymmdd(stamp))
            throw SystemExit{1, "--dates: " + util::quoted(stamp) +
                                    " is not a YYYYMMDD date. Expected e.g. --dates=20260827,20260825"};
        if (seen.insert(stamp).second) out.push_back(stamp);
    }
    if (out.empty()) throw SystemExit{1, "--dates was given but contained no dates"};
    std::sort(out.rbegin(), out.rend());
    return out;
}

std::vector<std::string> venue_selection(const std::vector<std::string>& raw) {
    if (raw.empty()) return {};
    std::set<std::string> known;
    for (const auto& [mic, v] : config::get().venues) known.insert(mic);
    std::vector<std::string> picked;
    std::set<std::string> unknown;
    for (const auto& value : raw)
        for (const auto& part : util::split(value, ",")) {
            std::string mic = util::upper(util::strip(part));
            if (mic.empty()) continue;
            if (known.count(mic)) {
                if (std::find(picked.begin(), picked.end(), mic) == picked.end()) picked.push_back(mic);
            } else {
                unknown.insert(mic);
            }
        }
    if (!unknown.empty())
        throw SystemExit{1, "Unknown venue(s): " + util::join({unknown.begin(), unknown.end()}, ", ") +
                                ". Venues come from [venue.<MIC>] sections in " + config::get().file.string() +
                                "; configured: " + (known.empty() ? "(none)" : util::join({known.begin(), known.end()}, ", "))};
    return picked;
}

int run(const std::vector<std::string>& argv) {
    try {
        auto g = split_global(argv);
        if (g.version) {
            std::cout << "premarket " << PREMARKET_VERSION << " (" << PREMARKET_BUILD_SHA << ")\n";
            return 0;
        }
        std::string command = g.rest.empty() ? "" : g.rest.front();
        std::vector<std::string> rest(g.rest.begin() + (g.rest.empty() ? 0 : 1), g.rest.end());
        if (g.help || command.empty()) {
            std::cout << (command.empty() ? kUsage : help_for(command));
            return command.empty() && !g.help ? 1 : 0;
        }
        if (g.config.empty()) throw args::UsageError{"--config=/full/path/config.ini is required"};
        if (!std::filesystem::path(g.config).is_absolute())
            throw args::UsageError{"--config must be an absolute path, got `" + g.config + "`"};
        if (!g.keys.empty() && !std::filesystem::path(g.keys).is_absolute())
            throw args::UsageError{"--keys must be an absolute path, got `" + g.keys + "`"};
        config::init(g.config, g.keys);
        return dispatch(command, rest);
    } catch (const args::UsageError& e) {
        log::err(std::string("usage: premarket --config=PATH [--keys=PATH] <command> [options]\npremarket: error: ") + e.message);
        return 2;
    } catch (const SystemExit& e) {
        if (!e.message.empty()) log::err(e.message);
        return e.code;
    } catch (const std::exception& e) {
        log::err(std::string("error: ") + e.what());
        return 1;
    }
}

int main(int argc, char** argv) {
    std::vector<std::string> args_v(argv + 1, argv + argc);
    return run(args_v);
}

}  // namespace premarket::cli
