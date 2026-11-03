#include "test_cli.hpp"

#include <doctest/doctest.h>

#include "premarket/cli.hpp"
#include "session_fixture.hpp"

using namespace premarket;

namespace {

struct Box : fixture::Tree {
    Box() {
        REQUIRE(state::init_state("test", false, fixture::quiet));
        for (const char* day : {"20261103"}) place_inputs(day);
    }
    void place_inputs(const std::string& day) const {
        fs::create_directories(data() / day);
        fs::copy(fixture::test_data("state7x/20261103/XNSE"), data() / day / "XNSE", fs::copy_options::recursive);
    }
    int run(std::vector<std::string> argv) const {
        argv.insert(argv.begin(), "--config=" + config_path.string());
        return cli::run(argv);
    }
    nlohmann::json header(const std::string& day) const {
        return jsonio::read(counter_token::venue_manifest_path(day, "XNSE"));
    }
};

std::vector<std::string> fill(const std::string& dates) {
    return {"normalize", "--dates=" + dates, "--venue", "XNSE", "--reason", "history before the first day", "--only",
            "normalize-fyers"};
}

}  // namespace

TEST_CASE("--config and --keys are required to be absolute") {
    fixture::Tree tree;
    fixture::Stderr err;
    CHECK(cli::run({"check-state"}) == 2);
    CHECK(err.text().find("--config=/full/path/config.ini is required") != std::string::npos);
    CHECK(cli::run({"--config=conf/config.ini", "check-state"}) == 2);
    CHECK(err.text().find("--config must be an absolute path, got `conf/config.ini`") != std::string::npos);
    CHECK(cli::run({"--config=" + tree.config_path.string(), "--keys=keys.ini", "check-state"}) == 2);
    CHECK(err.text().find("--keys must be an absolute path, got `keys.ini`") != std::string::npos);
}

TEST_CASE("usage errors exit 2 and say why") {
    Box box;
    fixture::Stderr err;
    CHECK(box.run({"normalize", "--date-dir", "20261103", "--csv-only"}) == 2);
    CHECK(err.text().find("unrecognized option: --csv-only") != std::string::npos);
    CHECK(box.run({"normalize", "--date-dir", "20261103", "--date-dir", "20261104"}) == 2);
    CHECK(err.text().find("--date-dir given twice") != std::string::npos);
    CHECK(box.run({"normalize", "--dates=20261102", "--date-dir", "20261103"}) == 2);
    CHECK(err.text().find("mutually exclusive") != std::string::npos);
    CHECK(box.run({"no-such-command"}) == 2);
}

TEST_CASE("a backfill needs its scope and its reason") {
    Box box;
    fixture::Stderr err;
    CHECK(box.run({"normalize", "--dates=20261102", "--reason", "r"}) == 1);
    CHECK(err.text().find("--dates needs --venue") != std::string::npos);
    CHECK(box.run({"normalize", "--dates=20261102", "--venue", "XNSE"}) == 1);
    CHECK(err.text().find("--dates needs --reason") != std::string::npos);
    CHECK_FALSE(fs::exists(counter_token::venue_manifest_path("20261102", "XNSE")));
}

TEST_CASE("a backfill fills newest first and passes the gate") {
    Box box;
    REQUIRE(box.run({"normalize", "--date-dir", "20261103", "--only", "normalize-fyers"}) == 0);
    auto live = counter_token::venue_entry("20261103", "XNSE")->assigned;
    REQUIRE_FALSE(live.empty());
    box.place_inputs("20261101");
    box.place_inputs("20261102");
    REQUIRE(box.run(fill("20261101,20261102")) == 0);
    CHECK(box.header("20261102")["numbering"]["neighbours"]["later"] == "20261103");
    CHECK(box.header("20261101")["numbering"]["neighbours"]["later"] == "20261102");
    CHECK(box.header("20261101")["numbering"]["reason"] == "history before the first day");
    CHECK(box.header("20261101")["numbering"]["mode"] == "fill");
    for (const char* day : {"20261101", "20261102"}) {
        CAPTURE(day);
        CHECK(counter_token::venue_entry(day, "XNSE")->assigned == live);
    }
    CHECK(session_fixture::event_types("quality-gate") == std::vector<std::string>{"COMPLETE"});
}

TEST_CASE("a backfill stops at a named venue with no input") {
    Box box;
    REQUIRE(box.run({"normalize", "--date-dir", "20261103", "--only", "normalize-fyers"}) == 0);
    box.place_inputs("20261101");
    fixture::Stderr err;
    CHECK(box.run(fill("20261101,20261102")) == 1);
    CHECK_FALSE(fs::exists(counter_token::venue_manifest_path("20261101", "XNSE")));
    CHECK_FALSE(fs::exists(counter_token::venue_manifest_path("20261102", "XNSE")));
    std::string error;
    for (const auto& e : state::read_events())
        if (e["job"]["name"] == "normalize" && e["eventType"] == "FAIL")
            error = e["run"]["facets"]["premarket"]["error"].get<std::string>();
    CHECK(error.find("no-input") != std::string::npos);
}

TEST_CASE("a backfill preview writes nothing") {
    Box box;
    REQUIRE(box.run({"normalize", "--date-dir", "20261103", "--only", "normalize-fyers"}) == 0);
    box.place_inputs("20261102");
    auto before = fixture::files_under(box.data());
    auto runs_before = util::read_file(state::runs_path());
    auto argv = fill("20261102");
    argv.push_back("--dry-run");
    CHECK(box.run(argv) == 0);
    CHECK(fixture::files_under(box.data()) == before);
    CHECK(util::read_file(state::runs_path()) == runs_before);
}
