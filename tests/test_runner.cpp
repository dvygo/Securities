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
}  // namespace

TEST_CASE("normalize's steps, and --only") {
    fixture::Tree tree;
    CHECK(names(runner::build_normalizer_steps({})) ==
          std::vector<std::string>{"normalize-fyers", "normalize-nse-contract", "normalize-mcx-contract",
                                   "normalize-databento", "baskets", "csv-export"});
    CHECK(names(runner::build_normalizer_steps({"baskets", "fyers"})) ==
          std::vector<std::string>{"normalize-fyers", "baskets"});
    CHECK(names(runner::build_normalizer_steps({"databento"})) == std::vector<std::string>{"normalize-databento"});
    CHECK(names(runner::build_normalizer_steps({"all"})).size() == 6);
    CHECK(names(runner::build_normalizer_steps({"normalize"})) ==
          std::vector<std::string>{"normalize-fyers", "normalize-nse-contract", "normalize-mcx-contract",
                                   "normalize-databento", "baskets"});
    try {
        runner::build_normalizer_steps({"baskets", "normalise-fyers"});
        FAIL("a misspelt step was accepted");
    } catch (const args::UsageError& e) {
        CHECK(e.message == "--only normalise-fyers: no such step (steps: normalize-fyers, normalize-nse-contract, "
                           "normalize-mcx-contract, normalize-databento, baskets, csv-export; aliases: fyers, "
                           "databento, normalize, all)");
    }
}

TEST_CASE("--venue limits the per-venue steps") {
    runner::Opts all;
    CHECK(runner::venue_selected(all, "XNSE"));
    runner::Opts some;
    some.venues = {"XNAS", "XCBO"};
    CHECK(runner::venue_selected(some, "xnas"));
    CHECK_FALSE(runner::venue_selected(some, "XNSE"));
}

TEST_CASE("download steps") {
    CHECK(names(runner::build_download_steps("india")) == std::vector<std::string>{"download-india"});
    CHECK(names(runner::build_download_steps("xcbo")) == std::vector<std::string>{"download-xcbo"});
}
