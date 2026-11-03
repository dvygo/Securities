#include "test_args.hpp"

#include <doctest/doctest.h>

#include "premarket/args.hpp"

using namespace premarket;

namespace {
const std::vector<args::Option> kOptions = {
    {"date-dir", true, false, "YYYYMMDD", ""},
    {"venue", true, true, "MIC", ""},
    {"dry-run", false, false, "", ""},
};

std::string usage_error(const std::vector<std::string>& argv, std::size_t positionals = 0) {
    try {
        args::parse(argv, kOptions, positionals);
    } catch (const args::UsageError& e) {
        return e.message;
    }
    return "no error";
}
}  // namespace

TEST_CASE("values, flags and repeats") {
    auto p = args::parse({"--date-dir=20261103", "--venue", "XNSE", "--venue=XBOM", "--dry-run"}, kOptions);
    CHECK(p.value("date-dir") == "20261103");
    CHECK(p.all("venue") == std::vector<std::string>{"XNSE", "XBOM"});
    CHECK(p.flag("dry-run"));
    CHECK_FALSE(p.value("missing"));
    auto q = args::parse({"--date-dir", "20261103"}, kOptions);
    CHECK_FALSE(q.flag("dry-run"));
    CHECK(q.all("venue").empty());
}

TEST_CASE("no abbreviations, no silent repeats, no stray values") {
    CHECK(usage_error({"--dat=20261103"}) == "unrecognized option: --dat");
    CHECK(usage_error({"--date-dir=1", "--date-dir=2"}) == "--date-dir given twice");
    CHECK(usage_error({"--dry-run=1"}) == "--dry-run takes no value");
    CHECK(usage_error({"--date-dir"}) == "--date-dir needs a value (YYYYMMDD)");
    CHECK(usage_error({"stray"}) == "unrecognized argument: stray");
    CHECK(usage_error({"one"}, 1) == "no error");
}
