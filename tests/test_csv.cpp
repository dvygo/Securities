#include "test_csv.hpp"

#include <doctest/doctest.h>

#include "premarket/csv.hpp"

using namespace premarket;

TEST_CASE("fields, quotes and doubled quotes") {
    CHECK(csv::parse_line("a,b,,d") == std::vector<std::string>{"a", "b", "", "d"});
    CHECK(csv::parse_line("\"a,b\",\"say \"\"hi\"\"\",c") == std::vector<std::string>{"a,b", "say \"hi\"", "c"});
    CHECK(csv::parse_line("").empty());
    CHECK(csv::parse_line("x,") == std::vector<std::string>{"x", ""});
}

TEST_CASE("records keep quoted line breaks and accept CRLF") {
    auto recs = csv::parse_records("h1,h2\r\n\"multi\nline\",2\r\nlast,3\n");
    REQUIRE(recs.size() == 3);
    CHECK(recs[1] == std::vector<std::string>{"multi\nline", "2"});
    CHECK(recs[2] == std::vector<std::string>{"last", "3"});
}

TEST_CASE("format_line quotes only what needs it, and parses back") {
    std::vector<std::string> fields = {"plain", "with,comma", "with \"quote\"", "line\nbreak", ""};
    auto line = csv::format_line(fields);
    CHECK(line == "plain,\"with,comma\",\"with \"\"quote\"\"\",\"line\nbreak\",");
    CHECK(csv::parse_records(line + "\n").at(0) == fields);
}
