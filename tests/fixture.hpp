#pragma once
// A throwaway premarket tree for tests: data/, contracts/, logs/, reports/, baskets/ and
// sinks/ under a fresh temp directory, plus a v8 config.ini naming them, installed as the
// process's configuration. Every test that touches disk builds one, so no test depends on
// another's files or on the machine it runs on.

#include <filesystem>
#include <random>
#include <string>

#include "premarket/config.hpp"
#include "premarket/normalize/counter_token.hpp"
#include "premarket/util.hpp"

namespace fixture {

namespace fs = std::filesystem;

inline std::string default_venues() {
    return "[venue.XNAS]\nfeed = databento\nvenue_id = 14\ndataset = EQUS.MINI\nstype_in = raw_symbol\n"
           "[venue.XCBO]\nfeed = databento\nvenue_id = 10\ndataset = OPRA.PILLAR\nstype_in = parent\n"
           "[venue.XCME]\nfeed = databento\nvenue_id = 12\ndataset = GLBX.MDP3\nstype_in = parent\n"
           "[venue.XNSE]\nfeed = fyers\nvenue_id = 16\n"
           "[venue.XBOM]\nfeed = fyers\nvenue_id = 18\n"
           "[venue.XIMC]\nfeed = fyers\nvenue_id = 20\n";
}

struct Tree {
    fs::path root;
    fs::path config_path;

    explicit Tree(const std::string& venues = default_venues(), const std::string& extra = "") {
        std::random_device rd;
        root = fs::temp_directory_path() / ("premarket-test-" + std::to_string(rd()) + std::to_string(rd()));
        fs::create_directories(root / "conf");
        for (const char* d : {"data", "contracts", "logs", "reports", "baskets", "sinks"})
            fs::create_directories(root / d);
        config_path = root / "conf" / "config.ini";
        write_config(venues, extra);
    }
    ~Tree() {
        std::error_code ec;
        fs::remove_all(root, ec);
        premarket::counter_token::reset_exchange_cache();
    }
    Tree(const Tree&) = delete;
    Tree& operator=(const Tree&) = delete;

    void write_config(const std::string& venues, const std::string& extra = "") {
        std::string text = "[premarket]\nconfig_version = 8\n[paths]\n";
        for (const char* d : {"data", "contracts", "logs", "reports", "baskets", "sinks"}) {
            std::string key = std::string(d) == "data"        ? "data_dir"
                              : std::string(d) == "contracts" ? "contracts_dir"
                              : std::string(d) == "logs"      ? "logs_dir"
                              : std::string(d) == "reports"   ? "reports_dir"
                              : std::string(d) == "baskets"   ? "baskets_dir"
                                                              : "sinks_dir";
            text += key + " = " + (root / d).string() + "\n";
        }
        text += "[run]\ntimezone = Asia/Kolkata\n" + extra + venues;
        premarket::util::write_file(config_path, text);
        premarket::config::init(config_path);
        premarket::counter_token::reset_exchange_cache();
    }

    fs::path data() const { return root / "data"; }
};

}  // namespace fixture
