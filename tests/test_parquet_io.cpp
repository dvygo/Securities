#include "test_parquet_io.hpp"

#include <doctest/doctest.h>

#include "fixture.hpp"
#include "premarket/parquet_io.hpp"

using namespace premarket;

TEST_CASE("strings round-trip exactly, and nothing is left behind") {
    fixture::Tree tree;
    auto file = tree.root / "out" / "t.parquet";
    std::vector<parquet::Row> rows = {{"a", ""}, {"en–dash", "quote\"comma,"}, {"", "line\nbreak"}};
    REQUIRE(parquet::write_rows(file, {"x", "y"}, rows));
    CHECK(fixture::files_under(file.parent_path()) == std::vector<std::string>{"t.parquet"});
    auto t = parquet::read_table(file);
    CHECK(t.columns == std::vector<std::string>{"x", "y"});
    REQUIRE(t.rows.size() == rows.size());
    for (std::size_t i = 0; i < rows.size(); ++i)
        for (std::size_t j = 0; j < 2; ++j) CHECK(t.rows[i][j] == rows[i][j]);
    CHECK(parquet::row_count(file) == 3);
    CHECK(parquet::column_names(file) == std::vector<std::string>{"x", "y"});
    auto y = parquet::read_table(file, {"y"});
    CHECK(y.columns == std::vector<std::string>{"y"});
    CHECK(y.rows[1][0] == "quote\"comma,");
}

TEST_CASE("typed tables keep nulls and read back as text") {
    fixture::Tree tree;
    auto file = tree.root / "typed.parquet";
    parquet::write_table(file, {{"script", parquet::Type::String}, {"token", parquet::Type::Int32}},
                         {{std::string("A"), std::string("7")}, {std::string("B"), std::nullopt}, {std::nullopt, std::string("-2147483648")}});
    auto t = parquet::read_table(file);
    REQUIRE(t.rows.size() == 3);
    CHECK(t.rows[0] == parquet::NullableRow{std::string("A"), std::string("7")});
    CHECK(t.rows[1] == parquet::NullableRow{std::string("B"), std::nullopt});
    CHECK(t.rows[2] == parquet::NullableRow{std::nullopt, std::string("-2147483648")});
    CHECK(t.index("token") == 1);
    CHECK(t.index("missing") == -1);
    CHECK(fixture::files_under(tree.root, {}) == std::vector<std::string>{"conf/config.ini", "typed.parquet"});
}

TEST_CASE("a writer replaces the target only on close, and abort leaves the old file") {
    fixture::Tree tree;
    auto file = tree.root / "day.parquet";
    REQUIRE(parquet::write_rows(file, {"v"}, {{"old"}}));
    {
        parquet::RowWriter w(file, {"v"});
        w.write({{"new1"}, {"new2"}});
        CHECK(parquet::read_table(file).rows.size() == 1);
        w.abort();
    }
    CHECK(parquet::read_table(file).rows[0][0] == "old");
    parquet::RowWriter w(file, {"v"});
    for (int i = 0; i < 5; ++i) w.write({{std::to_string(i)}});
    CHECK(w.total() == 5);
    CHECK(w.close());
    CHECK(parquet::row_count(file) == 5);
    CHECK(fixture::files_under(tree.root, {}) == std::vector<std::string>{"conf/config.ini", "day.parquet"});
}

TEST_CASE("iter_rows delivers fixed-size batches") {
    fixture::Tree tree;
    auto file = tree.root / "many.parquet";
    std::vector<parquet::Row> rows;
    for (int i = 0; i < 2500; ++i) rows.push_back({std::to_string(i), "x"});
    REQUIRE(parquet::write_rows(file, {"n", "x"}, rows));
    std::vector<std::size_t> sizes;
    std::size_t total = 0;
    parquet::iter_rows(file, 1000, [&](const std::vector<std::string>& cols, const std::vector<parquet::Row>& batch) {
        CHECK(cols == std::vector<std::string>{"n"});
        sizes.push_back(batch.size());
        total += batch.size();
    }, {"n"});
    CHECK(total == 2500);
    CHECK(sizes.front() == 1000);
}
