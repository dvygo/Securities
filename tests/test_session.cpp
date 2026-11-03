#include "test_session.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/normalize/session.hpp"

using namespace premarket;

TEST_CASE("IST sessions convert as 7.0.0 converted them") {
    for (const auto& c : fixture::golden("ist")) {
        CAPTURE(c["in"].get<std::string>());
        CHECK(session::trading_session_ist_to_utc(c["in"].get<std::string>()) == c["out"].get<std::string>());
    }
}

TEST_CASE("US sessions move with daylight saving as 7.0.0 moved them") {
    for (const auto& c : fixture::golden("us_sessions")) {
        auto d = c["date"].get<std::string>();
        CAPTURE(d);
        session::Ymd day{std::stoi(d.substr(0, 4)), std::stoi(d.substr(5, 2)), std::stoi(d.substr(8, 2))};
        CHECK(session::trading_session_for_xnas(day) == c["xnas"].get<std::string>());
        CHECK(session::trading_session_for_xcbo_equity(day) == c["xcbo_eq"].get<std::string>());
        CHECK(session::trading_session_for_xcbo_index(day) == c["xcbo_ix"].get<std::string>());
        CHECK(session::trading_session_for_xcme(day) == c["xcme"].get<std::string>());
        CHECK(session::xcbo_session_close_utc_seconds(day, true) == c["close_ix"].get<std::int64_t>());
        CHECK(session::xcbo_session_close_utc_seconds(day, false) == c["close_eq"].get<std::int64_t>());
    }
}
