#include "test_load.hpp"

#include <doctest/doctest.h>

#include "premarket/load.hpp"
#include "premarket/plugin.hpp"
#include "session_fixture.hpp"

using namespace premarket;

namespace {

const std::string LIVE = "20260926";
const std::string OLDER = "20260925";

const char* kVenues =
    "[venue.XCME]\nfeed = databento\nvenue_id = 12\ndataset = GLBX.MDP3\nstype_in = parent\n"
    "[venue.XNAS]\nfeed = databento\nvenue_id = 14\ndataset = EQUS.MINI\nstype_in = raw_symbol\n"
    "[venue.XCBO]\nfeed = databento\nenabled = 0\nvenue_id = 10\ndataset = OPRA.PILLAR\nstype_in = parent\n";

const char* kClickhouse = "type = clickhouse-normal\nhost = 127.0.0.1\nport = 1\ndatabase = d\nusername = u\npassword = p\nsecure = 0\n";

struct Box : fixture::Tree {
    Box() : fixture::Tree(kVenues) {
        counter_token::write_sequence(LIVE, counter_token::Sequence(2));
        counter_token::write_venue_manifest(LIVE, "XCME", {12, {{"A", 1}}, {}, {}});
        counter_token::write_venue_manifest(LIVE, "XNAS", {14, {{"B", 2}}, {}, {}});
        for (const auto& day : {OLDER, LIVE})
            for (const char* mic : {"XCME", "XNAS"}) normalized(day, mic);
        REQUIRE(state::init_state("test", false, fixture::quiet));
    }
    static void normalized(const std::string& day, const std::string& mic) {
        parquet::Row row(paths::NORMALIZED_COLUMNS.size());
        auto set = [&](const std::string& column, const std::string& value) {
            auto at = std::find(paths::NORMALIZED_COLUMNS.begin(), paths::NORMALIZED_COLUMNS.end(), column);
            row[static_cast<std::size_t>(at - paths::NORMALIZED_COLUMNS.begin())] = value;
        };
        set("script", "S");
        set("scriptToken", "7");
        set("def_raw_instrument_id", "7");
        set("counterTokenV2", mic == "XCME" ? "1" : "2");
        fs::create_directories(paths::normalized_dir(day));
        parquet::write_rows(paths::normalized_dir(day) / (mic + "-DATABENTO.parquet"), paths::NORMALIZED_COLUMNS, {row});
    }
    void sink(const std::string& name, const std::string& body) const {
        util::write_file(root / "sinks" / (name + ".ini"), "[sink]\n" + body);
    }
};

std::vector<jsonio::Json> load_events() {
    std::vector<jsonio::Json> out;
    for (const auto& e : state::read_events())
        if (e["job"]["name"] == "load") out.push_back(e);
    return out;
}

std::vector<std::string> kinds(const std::vector<jsonio::Json>& events) {
    std::vector<std::string> out;
    for (const auto& e : events) out.push_back(e["eventType"].get<std::string>());
    return out;
}

}  // namespace

TEST_CASE("sink files are strict") {
    Box box;
    box.sink("clickhouse-normal", kClickhouse);
    CHECK_THROWS_WITH_AS(load::load_sink("clickhose-normal"), doctest::Contains("configured: clickhouse-normal"), load::SinkError);
    box.sink("x", "type = kafka\n");
    CHECK_THROWS_WITH(load::load_sink("x"), doctest::Contains("unknown type"));
    box.sink("pg", "type = postgres-normal\n");
    CHECK_THROWS_WITH(load::load_sink("pg"), doctest::Contains("planned"));
    box.sink("p", "type = postgres-plugin\nmarket = XCME,XNAS\nvenue_id = 12\n");
    CHECK_THROWS_WITH(load::load_sink("p"), doctest::Contains("ONE market"));
    box.sink("p", "type = postgres-plugin\nmarket = XNAS\nvenue_id = 12\n");
    CHECK_THROWS_WITH(load::load_sink("p"), doctest::Contains("another's identity"));
    box.sink("p", "type = postgres-plugin\nmarket = XCBO\nvenue_id = 10\n");
    CHECK_THROWS_WITH(load::load_sink("p"), doctest::Contains("disabled"));
    box.sink("c", std::string(kClickhouse) + "passwd = x\n");
    CHECK_THROWS_WITH(load::load_sink("c"), doctest::Contains("passwd is not a known key"));
    box.sink("c", std::string(kClickhouse) + "[extra]\n");
    CHECK_THROWS_WITH(load::load_sink("c"), doctest::Contains("[extra] is not a known section (sink)"));
    CHECK(load::load_sink("clickhouse-normal").get("password") == "p");
}

TEST_CASE("every shipped template is a valid sink against the shipped config") {
    fixture::Tree tree;
    auto example = util::read_file(fixture::source_dir() + "/conf/config.ini.example");
    util::write_file(tree.config_path, util::replace_all(example, "/opt/securities", tree.root.string()));
    config::init(tree.config_path, fixture::source_dir() + "/conf/keys.ini.example");
    fs::create_directories(paths::sinks_dir());
    std::vector<std::string> names;
    for (const auto& t : util::list_dir_sorted(fixture::source_dir() + "/conf/sinks")) {
        auto name = t.filename().string();
        if (!util::ends_with(name, ".ini.example")) continue;
        names.push_back(name.substr(0, name.size() - std::string(".ini.example").size()));
        fs::copy_file(t, paths::sinks_dir() / (names.back() + ".ini"));
    }
    CHECK(names == std::vector<std::string>{"clickhouse-normal", "mdf-tokenmap", "postgres-plugin-xcbo",
                                            "postgres-plugin-xcme", "postgres-plugin-xnas"});
    CHECK(load::available() == names);
    std::set<std::string> types;
    for (const auto& n : names) types.insert(load::load_sink(n).type);
    CHECK(types == std::set<std::string>{load::CLICKHOUSE_NORMAL, load::POSTGRES_PLUGIN, load::MDF_TOKENMAP});
    CHECK_THROWS_WITH(config::databento_key("XNAS"), doctest::Contains("No Databento API key for XNAS"));
}

TEST_CASE("every sink is loaded before anything is pushed") {
    Box box;
    box.sink("ch", kClickhouse);
    CHECK_THROWS_AS(load::run(LIVE, {"ch", "missing"}, false), load::SinkError);
    CHECK(load_events().empty());
}

TEST_CASE("a dry run connects to nothing and logs nothing") {
    Box box;
    box.sink("ch", kClickhouse);
    auto before = state::read_events().size();
    CHECK(load::run(LIVE, {"ch"}, true) == 0);
    CHECK(state::read_events().size() == before);
}

TEST_CASE("load needs the state") {
    Box box;
    box.sink("ch", kClickhouse);
    fs::remove(state::head_path());
    CHECK_THROWS_AS(load::run(LIVE, {"ch"}, false), state::StateMissing);
}

TEST_CASE("the run log records each sink") {
    Box box;
    box.sink("mdf", "type = mdf-tokenmap\n");
    REQUIRE(load::run(LIVE, {"mdf"}, false) == 0);
    auto loads = load_events();
    CHECK(kinds(loads) == std::vector<std::string>{"START", "COMPLETE", "COMPLETE"});
    REQUIRE(loads.size() == 3);
    CHECK(loads[1]["run"]["facets"]["premarket"]["sink"] == "mdf");
    CHECK(loads[1]["run"]["facets"]["parent"]["run"]["runId"] == loads[0]["run"]["runId"]);
    for (const char* mic : {"XCME", "XNAS"}) {
        CAPTURE(mic);
        auto file = plugin::tokenmap::tokenmap_dir(LIVE) / plugin::tokenmap::filename(mic);
        REQUIRE(fs::exists(file));
        CHECK(plugin::tokenmap::verify(util::read_file(file), mic).entries == 1);
    }
    CHECK_FALSE(fs::exists(plugin::tokenmap::tokenmap_dir(LIVE) / plugin::tokenmap::filename("XCBO")));
}

TEST_CASE("a failing sink fails the load but the others still run") {
    Box box;
    box.sink("ch", kClickhouse);
    box.sink("mdf", "type = mdf-tokenmap\n");
    fixture::Stderr err;
    CHECK(load::run(LIVE, {"ch", "mdf"}, false) == 1);
    CHECK(kinds(load_events()) == std::vector<std::string>{"START", "FAIL", "COMPLETE", "FAIL"});
    CHECK(fs::exists(plugin::tokenmap::tokenmap_dir(LIVE) / plugin::tokenmap::filename("XNAS")));
}

TEST_CASE("an older day's token map never reaches MDF, but may go into its own tree") {
    Box box;
    auto vendor = box.root / "mdf-vendor";
    box.sink("mdf", "type = mdf-tokenmap\ndir = " + vendor.string() + "\n");
    box.sink("own", "type = mdf-tokenmap\n");
    {
        fixture::Stderr err;
        CHECK(load::run(OLDER, {"mdf"}, false) == 1);
    }
    std::string error;
    for (const auto& e : load_events())
        if (e["eventType"] == "FAIL" && e["run"]["facets"]["premarket"].contains("sink"))
            error = e["run"]["facets"]["premarket"]["error"].get<std::string>();
    CHECK(error.find("refusing to deliver") != std::string::npos);
    CHECK(fixture::files_under(vendor).empty());
    CHECK(load::run(OLDER, {"own"}, false) == 0);
    CHECK(fixture::files_under(plugin::tokenmap::tokenmap_dir(OLDER)) ==
          std::vector<std::string>{plugin::tokenmap::filename("XCME"), plugin::tokenmap::filename("XNAS")});
}
