#include "test_ini.hpp"

#include <doctest/doctest.h>

#include "premarket/ini.hpp"
#include "premarket/util.hpp"

using premarket::ini::File;

static const char* kSample = R"(# a comment
; another
[paths]
data_dir = /srv/data   # inline comment
url = postgres://u:p@h:6006/db?x=1;y=2
agent = "Mozilla/5.0 (X11; Linux) #not-a-comment"
empty =
only_comment =   # nothing set
[venue.XNAS]
enabled = 1
venue_id = 14
)";

TEST_CASE("values are read as written, minus comments") {
    auto f = File::parse(kSample, "sample.ini");
    const auto& paths = f.at("paths");
    CHECK(f.text(paths, "data_dir") == "/srv/data");
    // ';' with no whitespace before it is part of the value: URLs and query strings survive.
    CHECK(f.text(paths, "url") == "postgres://u:p@h:6006/db?x=1;y=2");
    CHECK(f.text(paths, "agent") == "Mozilla/5.0 (X11; Linux) #not-a-comment");
    CHECK(f.text(paths, "empty").empty());
    CHECK(f.text(paths, "only_comment").empty());
    CHECK(f.absolute_path(paths, "data_dir") == "/srv/data");
    const auto& xnas = f.at("venue.XNAS");
    CHECK(f.flag(xnas, "enabled"));
    CHECK(f.integer(xnas, "venue_id") == 14);
    CHECK(f.find("venue.xnas") == nullptr);
}

TEST_CASE("errors name the file and line") {
    auto msg = [](const std::string& text) {
        try {
            File::parse(text, "c.ini");
        } catch (const std::exception& e) {
            return std::string(e.what());
        }
        return std::string("no error");
    };
    CHECK(msg("[a]\nk=1\n[a]\n") == "c.ini:3: section [a] repeated (first at line 1)");
    CHECK(msg("[a]\nk=1\nk=2\n") == "c.ini:3: `k` repeated in [a] (first at line 2)");
    CHECK(msg("k=1\n") == "c.ini:1: `k` is outside any [section]");
    CHECK(msg("[a]\njust text\n") == "c.ini:2: expected `key = value`");
    CHECK(msg("[a]\nk = \"open\n") == "c.ini:2: unterminated quoted value for `k`");
}

TEST_CASE("typed reads refuse what they cannot mean") {
    auto f = File::parse("[s]\nflag = true\nn = 1.5\np = relative/dir\nx = 1\n", "t.ini");
    const auto& s = f.at("s");
    CHECK_THROWS_WITH(f.flag(s, "flag"), "t.ini:2: [s] flag must be 0 or 1, got `true`");
    CHECK_THROWS_WITH(f.integer(s, "n"), "t.ini:3: [s] n must be a whole number, got `1.5`");
    CHECK_THROWS_WITH(f.absolute_path(s, "p"), "t.ini:4: [s] p must be an absolute path, got `relative/dir`");
    CHECK_THROWS_WITH(f.only(s, {"flag", "n", "p"}), "t.ini:5: [s] x is not a known key (known: flag, n, p)");
    CHECK(f.integer(s, "missing", 7) == 7);
    CHECK_THROWS(f.required(s, "missing"));
}

TEST_CASE("a missing file is an error, not an empty config") {
    CHECK_THROWS_AS(File::read("/nonexistent/config.ini"), premarket::FileNotFoundError);
}
