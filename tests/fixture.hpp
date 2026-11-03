#pragma once
// A throwaway premarket tree for tests: data/, contracts/, logs/, reports/, baskets/ and
// sinks/ under a fresh temp directory, plus a v8 config.ini naming them, installed as the
// process's configuration. Every test that touches disk builds one, so no test depends on
// another's files or on the machine it runs on.

#include <fcntl.h>
#include <unistd.h>

#include <doctest/doctest.h>

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <map>
#include <random>
#include <string>
#include <vector>

#include "premarket/config.hpp"
#include "premarket/jsonio.hpp"
#include "premarket/paths.hpp"
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

inline std::string source_dir() { return PREMARKET_SOURCE_DIR; }
inline fs::path test_data(const std::string& relative = "") { return fs::path(source_dir()) / "tests" / "data" / relative; }

inline const premarket::jsonio::Json& golden(const std::string& section) {
    static const premarket::jsonio::Json all = premarket::jsonio::read(test_data("mappers_golden.json"));
    return all.at(section);
}

inline void quiet(const std::string&) {}

inline std::map<std::string, std::string> strings(const premarket::jsonio::Json& obj) {
    std::map<std::string, std::string> out;
    for (const auto& [k, v] : obj.items()) out[k] = v.get<std::string>();
    return out;
}

template <typename Row>
void same_leading_columns(const Row& row, const premarket::jsonio::Json& expected, std::size_t width) {
    for (std::size_t i = 0; i < width; ++i) {
        const auto& name = premarket::paths::NORMALIZED_COLUMNS[i];
        CAPTURE(name);
        CHECK(row.at(i) == expected.at(name).template get<std::string>());
    }
}

inline std::vector<std::string> files_under(const fs::path& root, const std::vector<std::string>& skip_names = {".lock"}) {
    std::vector<std::string> out;
    std::error_code ec;
    if (!fs::exists(root, ec)) return out;
    for (const auto& e : fs::recursive_directory_iterator(root))
        if (e.is_regular_file() &&
            std::find(skip_names.begin(), skip_names.end(), e.path().filename().string()) == skip_names.end())
            out.push_back(fs::relative(e.path(), root).string());
    std::sort(out.begin(), out.end());
    return out;
}

inline std::vector<std::string> files_ending(const fs::path& root, const std::string& suffix) {
    std::vector<std::string> out;
    for (const auto& f : files_under(root, {}))
        if (f.size() >= suffix.size() && f.compare(f.size() - suffix.size(), suffix.size(), suffix) == 0) out.push_back(f);
    return out;
}

struct Failpoint {
    explicit Failpoint(premarket::util::Failpoint fn) { premarket::util::set_failpoint(std::move(fn)); }
    ~Failpoint() { premarket::util::set_failpoint(nullptr); }
    Failpoint(const Failpoint&) = delete;
    Failpoint& operator=(const Failpoint&) = delete;
};

inline premarket::util::Failpoint crash_at(const std::string& point) {
    return [point](const std::string& name) {
        if (name == point) throw premarket::RuntimeError("simulated crash in " + point);
    };
}

struct Stderr {
    fs::path file;
    int saved = -1;
    Stderr() {
        std::random_device rd;
        file = fs::temp_directory_path() / ("premarket-stderr-" + std::to_string(rd()));
        std::fflush(stderr);
        saved = ::dup(2);
        int fd = ::open(file.c_str(), O_CREAT | O_TRUNC | O_WRONLY, 0600);
        ::dup2(fd, 2);
        ::close(fd);
    }
    std::string text() {
        std::fflush(stderr);
        return premarket::util::read_file(file);
    }
    ~Stderr() {
        std::fflush(stderr);
        ::dup2(saved, 2);
        ::close(saved);
        std::error_code ec;
        fs::remove(file, ec);
    }
    Stderr(const Stderr&) = delete;
    Stderr& operator=(const Stderr&) = delete;
};

}  // namespace fixture
