#include "test_runner.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/args.hpp"
#include "premarket/runner.hpp"

using namespace premarket;

namespace {
std::vector<std::string> names(const std::vector<runner::Step>& steps) {
    std::vector<std::string> out;
    for (const auto& s : steps) out.push_back(s.name);
    return out;
}

std::string usage_error(const std::string& venue, const std::vector<std::string>& only) {
    try {
        runner::build_normalizer_steps(venue, only);
    } catch (const args::UsageError& e) {
        return e.message;
    }
    return "no error";
}
}  // namespace

TEST_CASE("each venue's normalize steps come from its feed") {
    fixture::Tree tree;
    CHECK(names(runner::build_normalizer_steps("XNSE", {})) ==
          std::vector<std::string>{"normalize-fyers", "baskets", "csv-export"});
    CHECK(names(runner::build_normalizer_steps("XBOM", {})) == std::vector<std::string>{"normalize-fyers", "csv-export"});
    CHECK(names(runner::build_normalizer_steps("XNAS", {})) == std::vector<std::string>{"normalize-databento", "csv-export"});
    tree.write_config("[venue.XNSE]\nfeed = nse\nvenue_id = 16\n[venue.XIMC]\nfeed = mcx\nvenue_id = 20\n");
    CHECK(names(runner::build_normalizer_steps("XNSE", {})) ==
          std::vector<std::string>{"normalize-nse-contract", "baskets", "csv-export"});
    CHECK(names(runner::build_normalizer_steps("XIMC", {})) == std::vector<std::string>{"normalize-mcx-contract", "csv-export"});
    tree.write_config(fixture::default_venues(), "[baskets]\nenabled = 0\n");
    CHECK(names(runner::build_normalizer_steps("XNSE", {})) == std::vector<std::string>{"normalize-fyers", "csv-export"});
    CHECK(usage_error("XNSE", {"baskets"}).find("--only baskets: [baskets] enabled = 0") == 0);
}

TEST_CASE("--only picks among the venue's own steps, no aliases") {
    fixture::Tree tree;
    CHECK(names(runner::build_normalizer_steps("XNSE", {"baskets", "normalize-fyers"})) ==
          std::vector<std::string>{"normalize-fyers", "baskets"});
    CHECK(usage_error("XNSE", {"normalise-fyers"}) ==
          "--only normalise-fyers: not a step of XNSE (its steps: normalize-fyers, baskets, csv-export)");
    for (const char* gone : {"all", "normalize", "fyers", "databento"}) {
        CAPTURE(gone);
        CHECK(usage_error("XNAS", {gone}).find("not a step of XNAS") != std::string::npos);
    }
    CHECK(usage_error("XNAS", {"baskets"}) == "--only baskets: not a step of XNAS (its steps: normalize-databento, csv-export)");
}

TEST_CASE("--venue selects one MIC") {
    runner::Opts opts;
    opts.venue = "XNAS";
    CHECK(runner::venue_selected(opts, "xnas"));
    CHECK_FALSE(runner::venue_selected(opts, "XCBO"));
}

TEST_CASE("download steps, one per venue") {
    fixture::Tree tree;
    CHECK(names(runner::build_download_steps("XNSE")) == std::vector<std::string>{"download-xnse"});
    CHECK(names(runner::build_download_steps("XCBO")) == std::vector<std::string>{"download-xcbo"});
    tree.write_config("[venue.XIMC]\nfeed = mcx\nvenue_id = 20\n");
    CHECK_THROWS_WITH(runner::build_download_steps("XIMC"),
                      "XIMC is on the mcx feed: its contract file is placed in <data_dir>/YYYYMMDD/MCX/ by the BOD process, "
                      "not downloaded");
}
