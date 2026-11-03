#pragma once
// premarket/postgres.hpp -- the postgres-plugin sink.
//
// The plugin table in Postgres holds prices at the feed's scale, not premarket's: 100 for
// XCME and XNAS, 10000 for XCBO, and the India venues as they are. to_feed_scale() rescales
// strikeprice, ticksize and divisor and says when a value could not be rescaled exactly
// (it is then refused, never rounded); spot_fullname() gives XNAS spot rows their full name.
// Cases recorded from 7.0.0 (mappers_golden.json["feed_scale"]): every plugin row of
// test_plugin.cpp, through both.
//
// The CREATE TABLE and the upsert (COPY into a TEMP staging table, then INSERT ... SELECT
// DISTINCT ON the primary key ... ON CONFLICT DO UPDATE) are compared with the text 7.0.0
// generated (tests/data/postgres_sql.json): the column types and the conflict target are
// what existing plugin databases were created with.
