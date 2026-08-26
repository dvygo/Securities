# rust-viewer

TUI viewer for the formats this pipeline writes: Parquet, DBN(`.zst`) and raw
vendor CSV. Reads lazily up to `--limit` and formats only the visible window, so
startup does not scale with file size — the day's OPRA parquet is ~2M rows /
120 MB.

    rust-viewer <file.parquet|file.dbn.zst|file.csv> [--limit N]

Keys: arrows or `hjkl` move, `PgUp`/`PgDn` page, `g`/`G` top/bottom,
`0`/`$` first/last column, `q` or `Esc` quit.

## Build

Toolchain lives in `~/.rust` and is deliberately off PATH, matching `~/.python`
and `~/.jvm`:

    export RUSTUP_HOME=$HOME/.rust/rustup CARGO_HOME=$HOME/.rust/cargo
    export PATH=$HOME/.rust/cargo/bin:$PATH
    cargo build --release --features columnar

## Prerequisite: a C toolchain

    sudo apt install build-essential

Not optional, and not only for the optional features — cargo links its own build
scripts as host binaries and needs the `cc` driver for that, so even the CSV-only
build fails without it. Pointing rustc at the bundled `rust-lld` instead does not
work: it cannot resolve `-lgcc_s`, `-lutil` or `-lrt`, which are dev symlinks gcc
supplies. `libc6-dev` is already present on this box (crt1.o, crti.o, libc.so);
only the compiler driver is missing.

## Why `columnar` is a feature, not default

Everything behind it needs to compile C or C++:

- **Parquet** — the pipeline writes zstd-compressed Parquet, and the `zstd`
  crate builds the C library. There is no pure-Rust zstd decoder wired into
  arrow-rs, and the system `libzstd.so.1` is unusable for linking (no `.so` dev
  symlink, no headers, no pkg-config).
- **DBN** — `.dbn.zst` needs the same zstd, plus the `dbn` crate.
- **DuckDB** — `duckdb` with `bundled` compiles DuckDB's C++ amalgamation.

DuckDB drives everything that is *not* Parquet: CSV type-sniffing, filtering and
`ORDER BY` run in the query rather than in Rust. It cannot read DBN at all —
verified: `read_csv` and `read_parquet` both reject `.dbn.zst`, which is
Databento's own binary encoding — so DBN goes through the `dbn` crate.
