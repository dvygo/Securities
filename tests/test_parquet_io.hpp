#pragma once
// premarket/parquet_io.hpp -- every Parquet file premarket reads or writes goes through
// DuckDB's C API (libduckdb.so beside the binary).
//
// Writing: rows are appended to a table in a file-backed temporary .duckdb (threads = 1,
// memory_limit = 1GB, so a 900k-row day never needs that much RAM), then COPY'd to
// <file>.tmp.<pid> as zstd Parquet and renamed over the target, so a reader sees the old
// file or the new one, never a half-written one, and nothing is left behind on success or
// abort(). Normalized files are all strings; write_table() adds Int32 columns and nulls
// for the allocation, snapshot and exception tables.
//
// Reading: read_table() casts every column to VARCHAR and returns null as nullopt, so an
// Int32 column reads back as its decimal text; a column subset reads only those columns,
// in the order asked; iter_rows() delivers fixed-size batches.
//
// One DuckDB instance serves every read of a process while a parquet::Reader is alive;
// cli::main and the test runner's main each hold one, and it is closed when main returns.
// It must never be closed by exit(): 8.0.0's first cut kept the reader in a function-local
// static, and closing it among the exit handlers corrupted the heap in about one run in
// eight (glibc aborting in OPENSSL_cleanup, after doctest had reported success). Without a
// Reader, each read opens and closes its own instance, which is correct and ~15 ms slower.
