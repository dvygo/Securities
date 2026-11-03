#include "test_broker_script.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/broker_script.hpp"
#include "premarket/normalize/databento_norm.hpp"

using namespace premarket;

TEST_CASE("GLBX broker scripts agree with 7.0.0") {
    for (const auto& c : fixture::golden("broker")) {
        CAPTURE(c["script"].get<std::string>());
        CHECK(broker_script::from_glbx(c["script"].get<std::string>(), c["ns"].get<std::int64_t>()) ==
              c["out"].get<std::string>());
    }
}

TEST_CASE("OCC broker scripts agree with 7.0.0") {
    for (const auto& c : fixture::golden("occ")) {
        auto script = c["script"].get<std::string>();
        CAPTURE(script);
        CHECK(broker_script::from_occ(script, databento_norm::parse_occ_symbol(script)) == c["out"].get<std::string>());
    }
}

TEST_CASE("strikes are written in their shortest exact form") {
    for (const auto& c : fixture::golden("strike")) {
        CAPTURE(c["v"].dump());
        auto got = c["v"].is_string() ? broker_script::format_strike_text(c["v"].get<std::string>())
                                      : broker_script::format_strike(c["v"].get<double>());
        CHECK(got == c["out"].get<std::string>());
    }
}
