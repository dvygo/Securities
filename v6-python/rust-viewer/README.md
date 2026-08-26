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

## The C toolchain is clang/LLVM, not gcc

A C driver is required — cargo links its own build scripts as host binaries, so
even a CSV-only build needs one — but there is no gcc here and `sudo` wants a
password. `~/.zig` supplies clang 21 instead, wrapped by `~/.zig/shim/cc`, and
`.cargo/config.toml` points `CC`/`CXX`/`linker` at it. No gcc, no sudo, no
system packages.

Zig rather than an upstream LLVM tarball for two reasons: it is ~50 MB against
~1 GB, and it bundles its own libc headers and compiler-rt. Plain clang would
still fail on `-lgcc_s`, the dev symlink `libgcc-dev` supplies and this box
lacks. (Pointing rustc at the bundled `rust-lld` does not work either, for the
same reason.)

The shim exists because two translations are needed, both load-bearing:

  * `cc-rs` passes `--target=x86_64-unknown-linux-gnu`; zig parses only the
    vendor-less `x86_64-linux-gnu` and otherwise dies with
    `unable to parse target query ...: UnknownOperatingSystem`.
  * cargo build scripts inherit neither `HOME` nor `XDG_CACHE_HOME`, so
    `ZIG_GLOBAL_CACHE_DIR` has to be set outright or zig aborts with
    `AppDataDirUnavailable`.

If a build ever fails with "Folder 'zstd/lib' does not exists", the crates.io
extraction is corrupt, not the toolchain: delete
`~/.rust/cargo/registry/{src,cache}/*/zstd-sys-*` and re-run `cargo fetch`.

## Why `columnar` is a feature, not default

Everything behind it compiles C or C++, which is slower to build:

- **Parquet** — the pipeline writes zstd-compressed Parquet, and the `zstd`
  crate builds the C library. There is no pure-Rust zstd decoder wired into
  arrow-rs, and the system `libzstd.so.1` is unusable for linking (no `.so` dev
  symlink, no headers, no pkg-config).
- **DBN** — `.dbn.zst` needs the same zstd, plus the `dbn` crate.
- **DuckDB** — `duckdb` with `bundled` compiles DuckDB's C++ amalgamation, so it
  is a separate `duck` feature rather than part of `columnar`.

DuckDB drives everything that is *not* Parquet: CSV type-sniffing, filtering and
`ORDER BY` run in the query rather than in Rust. It cannot read DBN at all —
verified: `read_csv` and `read_parquet` both reject `.dbn.zst`, which is
Databento's own binary encoding — so DBN goes through the `dbn` crate.
