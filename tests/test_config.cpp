#include "test_config.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/config.hpp"

using namespace premarket;

TEST_CASE("the shipped example loads once install.sh has written the directory in") {
    fixture::Tree tree;
    auto example = util::read_file(std::string(PREMARKET_SOURCE_DIR) + "/conf/config.ini.example");
    util::write_file(tree.config_path, util::replace_all(example, "@DIR@", "/srv/securities"));
    auto cfg = config::load(tree.config_path);
    CHECK(cfg.paths.data_dir == "/srv/securities/state/data");
    CHECK(cfg.paths.baskets_dir == "/srv/securities/share/baskets");
    CHECK(cfg.timezone == "Asia/Kolkata");
    CHECK(cfg.baskets_enabled);
    REQUIRE(cfg.venues.size() == 6);
    // The ids every 7.x host numbered under; changing one would be refused by the state.
    CHECK(cfg.venues.at("XCBO").venue_id == 10);
    CHECK(cfg.venues.at("XCME").venue_id == 12);
    CHECK(cfg.venues.at("XNAS").venue_id == 14);
    CHECK(cfg.venues.at("XNSE").venue_id == 16);
    CHECK(cfg.venues.at("XBOM").venue_id == 18);
    CHECK(cfg.venues.at("XIMC").venue_id == 20);
    CHECK(cfg.venues.at("XCBO").stype_in == "parent");
    CHECK(cfg.venues.at("XNAS").symbols == "/srv/securities/conf/symbols/XNAS.txt");
    CHECK(cfg.fyers.user_agent.find("Win64; x64") != std::string::npos);
}

TEST_CASE("a 7.x config is refused, not half-read") {
    fixture::Tree tree;
    util::write_file(tree.config_path, "[paths]\ndata_dir = premarket/data\n[EXCHANGE:XNAS]\nfeed = databento\n");
    CHECK_THROWS_WITH(config::load(tree.config_path),
                      doctest::Contains("no [premarket] section. This is not a premarket 8 config"));
}

TEST_CASE("relative paths are refused everywhere") {
    CHECK_THROWS_WITH(config::load("conf/config.ini"), "--config must be an absolute path, got `conf/config.ini`");
    CHECK_THROWS_WITH(config::load_keys("keys.ini"), "--keys must be an absolute path, got `keys.ini`");
    fixture::Tree tree;
    auto text = util::read_file(tree.config_path);
    util::write_file(tree.config_path, util::replace_all(text, "data_dir = " + (tree.root / "data").string(),
                                                         "data_dir = state/data"));
    CHECK_THROWS_WITH(config::load(tree.config_path), doctest::Contains("data_dir must be an absolute path"));
}

TEST_CASE("unknown sections, keys, feeds and zones are errors") {
    fixture::Tree tree;
    CHECK_THROWS_WITH(tree.write_config(fixture::default_venues(), "[databento]\nlive_seconds = 10\n"),
                      doctest::Contains("[databento] is not a known section"));
    CHECK_THROWS_WITH(tree.write_config("[venue.XNAS]\nfeed = databento\ndataset = EQUS.MINI\nschema = definition\n"),
                      doctest::Contains("[venue.XNAS] schema is not a known key"));
    CHECK_THROWS_WITH(tree.write_config("[venue.XNAS]\nfeed = csv\n"), doctest::Contains("one of databento, fyers, nse, mcx"));
    CHECK_THROWS_WITH(tree.write_config("[venue.XNSE]\nfeed = fyers\ndataset = X\n"),
                      doctest::Contains("only applies to feed = databento"));
    CHECK_THROWS_WITH(tree.write_config("[venue.xnas]\nfeed = fyers\n"), doctest::Contains("is not a four-letter upper-case MIC"));
    auto text = util::read_file(tree.config_path);
    util::write_file(tree.config_path, util::replace_all(text, "timezone = Asia/Kolkata", "timezone = Mars/Olympus"));
    CHECK_THROWS_WITH(config::load(tree.config_path), doctest::Contains("is not a timezone in the tz database"));
}

TEST_CASE("keys come only from the --keys file") {
    fixture::Tree tree;
    CHECK_THROWS_WITH(config::databento_key("XNAS"), "no keys file: pass --keys=/full/path/keys.ini to download XNAS");
    auto keys = tree.root / "conf" / "keys.ini";
    util::write_file(keys, "[databento]\nXNAS = db-test\nXCBO =\n");
    config::init(tree.config_path, keys);
    CHECK(config::databento_key("xnas") == "db-test");
    CHECK_THROWS_WITH(config::databento_key("XCBO"), doctest::Contains("No Databento API key for XCBO"));
    util::write_file(keys, "[production]\nkey_XNAS = db-test\n");
    CHECK_THROWS_WITH(config::load_keys(keys), doctest::Contains("[production] is not a known section (databento)"));
}

TEST_CASE("feed ownership") {
    fixture::Tree tree;
    CHECK(config::owns("XNSE", "fyers"));
    CHECK_FALSE(config::owns("XNSE", "nse"));
    CHECK(config::feed_for("xcme") == "databento");
    CHECK(config::feed_for("XLON").empty());
    CHECK(config::today().size() == 8);
}
