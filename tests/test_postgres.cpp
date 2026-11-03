#include "test_postgres.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/postgres.hpp"

using namespace premarket;

TEST_CASE("feed scale and spot names agree with 7.0.0") {
    for (const auto& c : fixture::golden("feed_scale")) {
        auto in = fixture::strings(c["in"]);
        CAPTURE(in["exch"]);
        CAPTURE(in["name"]);
        auto scaled = postgres::to_feed_scale(in);
        postgres::spot_fullname(scaled.row);
        CHECK(scaled.row == fixture::strings(c["out"]));
        CHECK(scaled.inexact == c["inexact"].get<bool>());
    }
}

TEST_CASE("the generated SQL is 7.0.0's, word for word") {
    auto expected = jsonio::read(fixture::test_data("postgres_sql.json"));
    CHECK(postgres::create_table_sql("public", "contracts") == expected["create"].get<std::string>());
    CHECK(postgres::upsert_sql("public", "contracts") == expected["upsert"].get<std::string>());
}
