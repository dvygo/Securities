#include "test_util.hpp"

#include <doctest/doctest.h>

#include <cmath>

#include "premarket/util.hpp"

using namespace premarket;

TEST_CASE("parse_int reads a whole number") {
    CHECK(util::parse_int(" 12 ") == 12);
    CHECK(util::parse_int("+5") == 5);
    CHECK(util::parse_int("-0") == 0);
    CHECK(util::parse_int("010") == 10);
    CHECK_FALSE(util::parse_int("1.0"));
    CHECK_FALSE(util::parse_int(""));
    CHECK_FALSE(util::parse_int("12a"));
}

TEST_CASE("parse_float reads decimals and refuses hex") {
    CHECK(util::parse_float("1e5") == 100000.0);
    CHECK(util::parse_float(" 2.5 ") == 2.5);
    CHECK(std::isinf(*util::parse_float("inf")));
    CHECK(*util::parse_float("-Infinity") < 0);
    CHECK(std::isnan(*util::parse_float("nan")));
    CHECK(util::parse_float(".5") == 0.5);
    CHECK(util::parse_float("5.") == 5.0);
    // strtod alone would accept hex; a strike of "0x10" must stay unparsed.
    CHECK_FALSE(util::parse_float("0x10"));
    CHECK_FALSE(util::parse_float("1e"));
    CHECK(util::parse_float("+.5e-3") == 0.0005);
    CHECK(util::parse_int_truncating("75.0") == 75);
    CHECK(util::parse_int_truncating("-2.9") == -2);
    CHECK_FALSE(util::parse_int_truncating("nan"));
}

TEST_CASE("round_half_even rounds a scaled price the way 7.x did") {
    CHECK(util::round_half_even(0.5) == 0);
    CHECK(util::round_half_even(1.5) == 2);
    CHECK(util::round_half_even(2.5) == 2);
    CHECK(util::round_half_even(-2.5) == -2);
    CHECK(util::round_half_even(1.005 * 100) == 100);
}

TEST_CASE("commas groups thousands") {
    CHECK(util::commas(0) == "0");
    CHECK(util::commas(999) == "999");
    CHECK(util::commas(1000) == "1,000");
    CHECK(util::commas(140537) == "140,537");
    CHECK(util::commas(-1234567) == "-1,234,567");
}

TEST_CASE("string helpers") {
    CHECK(util::strip("  a b \t\n") == "a b");
    CHECK(util::split("a,,b", ",") == std::vector<std::string>{"a", "", "b"});
    CHECK(util::split_ws("  a  b ") == std::vector<std::string>{"a", "b"});
    CHECK(util::splitlines("a\r\nb\nc") == std::vector<std::string>{"a", "b", "c"});
    CHECK(util::quoted("it's") == "\"it's\"");
    CHECK(util::quoted("abc") == "'abc'");
}

TEST_CASE("YYYYMMDD arithmetic is calendar-correct") {
    CHECK(util::add_days("20260301", -1) == "20260228");
    CHECK(util::add_days("20241231", 1) == "20250101");
    CHECK(util::days_between("20261001", "20261031") == 30);
    CHECK(util::day_start_ns("19700102") == 86400LL * 1000000000LL);
    CHECK_FALSE(util::valid_yyyymmdd("20260230"));
    CHECK(util::valid_yyyymmdd("20240229"));
}
