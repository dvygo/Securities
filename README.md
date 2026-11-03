# Securities

Symbology and basket pipeline for US and India markets — downloads each venue's
contract master, normalizes every venue into a common schema, assigns a stable
cross-venue instrument token, and pushes the day to its consumers.

US venues come from Databento (`GLBX.MDP3`, `EQUS.MINI`, `OPRA.PILLAR`). India
comes from NSE's own contract masters or Fyers for `XNSE`, from MCX's contract
file or Fyers for `XIMC`, and from Fyers for `XBOM`.

The pipeline, `premarket`, runs in three stages:

1. **download** each venue's contract master;
2. **normalize** every venue into one schema and assign a stable, cross-venue
   instrument token (`counterTokenV2`) -- files only, never a database;
3. **load** the day into its consumers -- ClickHouse, the Postgres symbol
   master, MDF's token map -- through named sinks.

Since 8.0.0 `premarket` is a single C++20 binary for Ubuntu 22.04. Docker is
used to build it and never to run it: a host needs only the release tarball and
`tzdata`. 8.0.0 reads the numbering state and the manifests of 7.x after a
one-time `premarket migrate-state`, and numbers every venue exactly as 7.0.0
did (see [RUNBOOK.md, "Upgrading to 8.0.0"](RUNBOOK.md#upgrading-to-800)).

Design decisions and their reasons are in
[`docs/DESIGN/OLD-DATE-PROCESSING.md`](docs/DESIGN/OLD-DATE-PROCESSING.md).

## Layout

| Path | What it is |
|------|------------|
| [`apps/`](apps/), [`include/premarket/`](include/premarket/), [`src/`](src/) | The pipeline: `main()`, the headers, the implementation |
| [`tests/`](tests/) | The test suite (doctest). `tests/test_<module>.hpp` documents each module; `tests/data/` holds what 7.0.0 produced, which 8.0.0 must reproduce |
| [`conf/`](conf/) | `config.ini`, `keys.ini` and `sinks/<name>.ini` templates (`*.example`), and the per-venue symbol lists in `symbols/` |
| [`constituents/baskets/`](constituents/baskets/) | Basket definitions |
| [`docs/`](docs/) | Design records and the plugin table's column types |
| [`docker/build.Dockerfile`](docker/build.Dockerfile) | The build image, `securities-build:22.04`: clang 18 + libc++ and every library at a pinned version |
| [`docker/contract-*`](docker/) | Throwaway ClickHouse and Postgres the load sinks can push into |
| [`build.sh`](build.sh) | Build and test inside the build image |
| [`packaging/`](packaging/) | The release build (`release.sh`) and publish (`publish.sh`) for `deshik-ux/securities` |
| [`deploy/`](deploy/) | Installing and running a release on a host: [SETUP](deploy/SETUP.md), [RUNBOOK](deploy/RUNBOOK.md) |
| [`RUNBOOK.md`](RUNBOOK.md) | The daily runbook for the production box |

## Building

```bash
./build.sh             # configure, build, run the tests -> build/premarket
./build.sh sanitize    # the tests again under AddressSanitizer + UBSan
./build.sh shell       # a shell in the build image, repo at /src
packaging/release.sh   # the release tarball, tested on a bare ubuntu:22.04 -> dist/<version>/
```

The first run builds `securities-build:22.04` from `docker/build.Dockerfile`
(Ubuntu 22.04, clang 18, libc++, CMake 3.30.5, databento-cpp 0.69.0, DuckDB
1.5.5, cpp-httplib 0.62.0, nlohmann/json 3.12.0, HH date 3.0.5, doctest 2.4.12,
and libpq from PostgreSQL 18.6, linked statically). The binary links libc++
statically and loads `libduckdb.so` from `../lib` beside it; everything else it
needs (glibc, OpenSSL 3, zlib, zstd) is in a stock Ubuntu 22.04.

## Running

```
premarket --config=/abs/config.ini [--keys=/abs/keys.ini] <command> [options]
```

Every run is for one exchange, named with `--venue MIC`. There is no run for
"all venues": a venue's day is its own download, normalize and load.

| Command | What it does |
|---|---|
| `download --venue MIC` | download the venue's contract master: the Databento live replay of today's session (XNAS, XCME, XCBO), or its Fyers symbol master (XNSE, XBOM, XIMC) |
| `normalize --venue MIC` | normalize the venue's day and number it (`counterTokenV2`); `--dates` fills older days |
| `load --venue MIC` | deliver the venue's normalized day through the sinks that carry it |
| `check-tokens --venue MIC`, `check-lineage --venue MIC` | validate the venue's numbering and lineage |
| `check-state` | audit the numbering state, which every venue shares |
| `init-state` | create the numbering state, once per host |
| `migrate-state` | move a 7.x numbering state and its manifests to v8, once |

`--config` is required by every command and `--keys` by the downloads. Both take
an absolute path, always: a cron job or an HTCondor job runs in a directory
nobody chose. `premarket <command> --help` lists a command's options. Exit status
is 0 on success, 1 on a failure, 2 on a command-line mistake.

Nothing is read from the environment. Every setting is in `config.ini`, every
key in `keys.ini`, every sink in `<sinks_dir>/<name>.ini`, and each file is
strict: an unknown section or key is an error naming the file and line.

## Venues and sources

| MIC | Market | Source | Definitions out (UTC, US winter time) |
|-----|--------|--------|------------------------|
| `XCME` | CME Globex futures/options | Databento `GLBX.MDP3` | from Sunday afternoon |
| `XNAS` | US equities | Databento `EQUS.MINI` | ~06:00–07:00 |
| `XCBO` | US options | Databento `OPRA.PILLAR` | session ~11:30, new listings ~13:00 |
| `XNSE` | NSE India (cash, F&O, currency) | Fyers *or* NSE contract masters — see below | — |
| `XBOM` | BSE India | Fyers | — |
| `XIMC` | MCX India | Fyers *or* MCX's own contract file — see below | — |

Databento definitions are a **live** replay of the current session: a download
is only ever today's, and it is refused until the day's re-send has happened.
History before the first day is filled from files placed by hand (below).

`XNSE` is the one venue two feeds can serve, and `config.ini` picks which:

```ini
[venue.XNSE]
feed = fyers      # Fyers CDN (NSE_CM/FO/CD)      -> XNSE-FYERS.parquet
# feed = nse      # NSE's own contract masters    -> XNSE-NSE.parquet
```

That value is the whole switch. A normalize step runs a venue only when it owns
it, so the two never both write `XNSE`, and baskets resolve the filename through
the same setting rather than assuming a vendor. Under `feed = fyers` the
`NEW FILE FORMAT/` drop is ignored; under `feed = nse` the Fyers NSE segments are
not downloaded. Switching costs no tokens — the carry-forward keys on the script,
not the source, so a swap keeps every symbol's `counterTokenV2` and draws nothing
new from the shared sequence.

`XIMC` has the same kind of switch:

```ini
[venue.XIMC]
feed = fyers      # Fyers CDN (MCX_COM.csv)       -> XIMC-FYERS.parquet
# feed = mcx      # MCX's own contract file       -> XIMC-MCX.parquet
```

Under `feed = mcx` the BOD process places one zip a day in
`data/YYYYMMDD/MCX/`, and `normalize-mcx-contract` reads `MCXScrips.bcp` from
inside it. The column layout, how it was established, and where MCX's own spec
lives are in `docs/DESIGN/MCX-CONTRACT-FILE.md`.
Contracts are named without a prefix: `GOLD26OCT166000CE`, `GOLD27AUGFUT`, and
`GOLD` for a spot row.
Expired contracts and calendar spreads are dropped, and a file dated before the
run day is warned about. Two differences from every other venue:

- **`counterTokenV2` is MCX's own token**, and XIMC is not numbered in
  `data/_state/` -- no plan, no manifest, nothing for `check-tokens` to check. It
  can collide with another venue's `counterTokenV2` in a shared `resultset`.
- **Trading hours and the expiry time come from the commodity group**: metals,
  energy and indices 09:00-23:30 IST (23:55 while the US is on standard time),
  agri 09:00-17:00, and COTTON, CPO, KAPAS and MENTHAOIL 09:00-21:00 (21:30).

**The venues do not arrive together.** Run normalize as often as you like: each
run picks up whatever has landed without disturbing what is already numbered.

## Quick start

Install a release (see [deploy/SETUP.md](deploy/SETUP.md)), or build one, then:

```bash
PM="/opt/securities/current/bin/premarket --config=/opt/securities/conf/config.ini --keys=/opt/securities/conf/keys.ini"
$PM init-state --reason "first run on this host"      # once per host; 7.x hosts run migrate-state instead
```

A venue's day -- download, normalize, load -- and the venues one at a time, in
the order new contracts are numbered:

```bash
for v in XNAS XCME XCBO XNSE XBOM XIMC; do
    $PM download  --venue $v
    $PM normalize --venue $v --reason "daily $v"
    $PM load      --venue $v          # every sink that carries $v; or --sink NAME ...
done
```

**Older days** -- history before the first day, or a day that was missed. A
download is today's live replay only, so put each day's definition file (live
instrument ids) in `data/YYYYMMDD/<MIC>/` by hand, then preview and fill:

```bash
$PM normalize --venue XCME --dates=20260924,20260923 --reason "history before go-live" --dry-run
$PM normalize --venue XCME --dates=20260924,20260923 --reason "history before go-live"
```

A fill runs newest first, takes a backup first, and ends with a quality gate
(check-tokens + check-state). Never `normalize --date-dir` an older date: it is
refused, because numbering it as a live day would fork the token chain.

## Layout of a day

```
<data_dir>/YYYYMMDD/
  XCME/  glbx-mdp3-YYYYMMDD.definition.dbn.zst      live definition replay (live ids)
  XNAS/  equs-mini-YYYYMMDD.definition.dbn.zst
  XCBO/  opra-pillar-YYYYMMDD.definition.dbn.zst
  XNSE/  XNSE-FYERS.csv XNFO-FYERS.csv XNCD-FYERS.csv   when feed = fyers
         NEW FILE FORMAT/                            when feed = nse
  TRANSFORM/                                       everything derived from the above
    normalized/   <MIC>-<SOURCE>.parquet             one file per venue
    plugin/       <MIC>-<SOURCE>.parquet             legacy symbol-master shape
                  tokenmap/tokenmap.<MIC>.bin        MDF's binary token map
    manifests/    <MIC>.json                         header: the completion record
                  <MIC>.alloc.parquet                the venue's token allocation
                  <MIC>.exceptions.parquet           a fill's recorded breaks, if any
                  _sequence.json                     the counter as this day left it

<data_dir>/_state/                                 the numbering state (see below)
  manifest.json                                    the head: counter, last run, venues
  <MIC>/<run_id>.alloc.parquet                     state snapshots, never modified
  runs.jsonl                                       the run log (OpenLineage-shaped)
  backups/<run_id>.tar.gz                          before every fill, init-state and migrate-state

<logs_dir>/<command>-<MIC>_<date>_<stamp>.log      one log per run
<reports_dir>/<check>[-<MIC>].{v2,ALL}.txt         the latest report of each check, per venue
<contracts_dir>/YYYYMMDD/                          basket contracts
```

## counterTokenV2

One integer sequence, shared by every venue, starting at 1. `scriptToken` carries
each source's own instrument id, which is only unique within that source — on
2026-08-12 the raw ids collided 932 times between XCME and XNAS. Anything keying
on a token without an exchange column needs something collision-free.

Three rules, applied per venue against its latest state:

- a script that is still listed **keeps** its token;
- a script that has gone **releases** its token into that venue's own pool;
- an arrival **drains that pool first**, and only then draws a fresh number from
  the shared sequence.

Only the counter is global. Pools stay per venue, which is what keeps a venue's
numbering explicable from its own manifest and keeps the sequence growing far
slower than the arrival count. Over 2026-08-24..09-01 the whole estate — six
venues, roughly 3.2M instruments a day — consumed 3,396,189 of the 2.1 billion
int32 numbers, and Monday 08-31 was numbered *entirely* from weekend expiry with
no venue drawing at all.

**The counter never restarts** and never hands out a number twice, across all
runs and dates: every run draws above the highest number on disk and in the
state, whatever the date it is numbering.

**Numbering is append-only within a trade date.** A token handed out on a date is
never given to a different instrument on that date -- not by a re-run with fewer
symbols, not by a second vendor for the same market later the same day. What a
later pass no longer carries is `retained`, and freed only when the date moves
on. (The Postgres push upserts on `(token, trade_date)`; reusing a number within
a date would swap two instruments' identities.)

**Re-running a day is safe.** A live run continues from the venue's latest state,
which already holds the day, so a second pass keeps every token and numbers only
what is genuinely new. A script missing from an earlier pass reclaims its own
previous token rather than the pool's lowest.

**Older days are filled, not chained.** `normalize --dates` numbers a day from the
numbered days either side of it: a script on the later day keeps that token (the
later day wins, because that is the chain that leads to today), one only on the
earlier day keeps that one, and everything else takes a free number or a new
one. A fill never releases and never changes a token the state holds, so no live
instrument's token can move. Where the two neighbours cannot both be honoured
-- an instrument expired, and its number was recycled to a new listing the next
day -- the loser gets a new number on the filled day only, and the break is
recorded in `<MIC>.exceptions.parquet`.

## Manifests

A venue's header is written only after its normalized Parquet is promoted, so
**its presence is the answer to "is this venue done for this date"**. Absent means
not done yet; it never means empty.

```jsonc
{
 "version": 8,
 "date": "20261103", "venue": "XCBO",
 "started_at": "...Z", "completed_at": "...Z",   // when this venue actually ran
 "code":       { "build_sha": "...", "manifest_version": 8 },
 "allocation": { "venue_id": 10, "count": 2007183, "free_count": 69068, "highest": 3367460,
                 "path": "XCBO.alloc.parquet", "sha256": "..." },
 "tokens":     { "carried_from": "20261102", "day": { "arrived": 7937, "released": 9396, "drawn": 0, ... },
                 "run": { "sequence_before": 3367460, "sequence_after": 3367460, ... } },
 "inputs":     [{ "path": "XCBO/opra-pillar-...dbn.zst", "sha256": "..." }],
 "outputs":    [{ "path": "TRANSFORM/normalized/...parquet", "rows": 2007183, "sha256": "..." }],
 "numbering":  { "mode": "live", "run_id": "...", "reason": "...", "operator": "user@host",
                 "state_before": { "snapshot": "XCBO/<run>.alloc.parquet", "sha256": "..." },
                 "state_after":  { "snapshot": "XCBO/<run>.alloc.parquet", "sha256": "..." },
                 "neighbours": { "earlier": "20261102" }, "counter_before": 3367460,
                 "counter_after": 3367460, "exceptions": { "count": 0 } }
}
```

The allocation itself lives in `<MIC>.alloc.parquet` (`script`, `token`, `state`)
rather than inline. Assigned and free share one file deliberately: they are two
halves of one invariant, and splitting them would let a crash leave an allocation
with an empty pool, which the next day would read as "nothing to recycle".

Version 8 is version 5 (7.x) with nothing removed. A header that
`migrate-state` moved from 7.x also carries
`"migrated": {"from_version": 5, "by", "at", "run_id"}`. 8.0.0 refuses to number
against a version-5 header or a 7.x head and names `migrate-state` in the
refusal; it never rewrites one on the fly.

## The numbering state

`<data_dir>/_state/` is what every numbering run starts from and leaves behind:
per venue, the tokens it holds now and the ones it may hand out. It is created
once per host by `init-state` (from each venue's newest manifest; it refuses if
two venues share a token) -- or, on a 7.x host, moved to v8 by `migrate-state` --
and after that `normalize` and `load` refuse to run without it.

- **One run at a time.** Every numbering or load run holds `_state/.lock`; a
  second one exits at once, naming the holder.
- **Crash-safe.** A run reserves its numbers before publishing a file, stages its
  manifests and snapshot, and commits by replacing the head; the next run's
  recovery finishes or discards whatever an interrupted run left. Numbers can
  leak; they are never handed out twice.
- **Audited.** `runs.jsonl` records every run -- START, COMPLETE or FAIL, the
  operator, the reason, what it read and wrote with digests -- in the OpenLineage
  shape, so it can be fed to a lineage catalog.
- **Backed up.** Every fill, `init-state` and `migrate-state` first writes
  `backups/<run_id>.tar.gz` (every day's manifests and the head; the newest ten
  are kept). Restore from the data root: `tar -xzf _state/backups/<run_id>.tar.gz`.

## Load

`premarket load --venue MIC [--date-dir D] [--sink NAME ...]` delivers one
venue's normalized day, by default through every sink in `sinks_dir` that
carries that venue. Each sink is one `<name>.ini` with a `[sink]` section (copy a
`conf/sinks/*.ini.example`):

| type | keys | carries | what it delivers |
|---|---|---|---|
| `clickhouse-normal` | `host port database username password secure` | every venue | the canonical schema into `contracts_YYYYMMDD`/`baskets_YYYYMMDD` and the current `contracts`/`baskets`, replacing only this venue's rows (its contracts, and its baskets, whose names start with its MIC) |
| `postgres-plugin` | `market venue_id database_url schema table create_table` | its `market` | the legacy 17-column symbol master, upserted on `(token, trade_date)`; its `venue_id` must match config.ini's |
| `mdf-tokenmap` | `markets dir` | Databento venues in `markets` (empty = all) | `tokenmap.<MIC>.bin`, into the day's tree or MDF's `dir` |

Loading the venues one by one builds the whole day in ClickHouse; the current
mirrors hold each venue's newest live day. Every chosen sink is read and
validated before anything is pushed. A day older than the venue's newest live
day never becomes "current": it loads its dated ClickHouse tables but not the
mirrors, and is refused for MDF's delivery directory. `--dry-run` validates the
sinks and connects to nothing.

## Validation

```bash
$PM check-tokens  --venue XCME --dates=20260824,20260825,20260826
$PM check-lineage --venue XCBO --dates=20260901
$PM check-state
```

`check-tokens` pins the numbering: tokens populated, numeric and inside int32,
one-to-one with scripts, disjoint from every other venue's on the same day,
covered by the sequence, and
agreeing with the manifest. Across a pair of days it also proves the recycling
actually fired — that departures released, that arrivals drained the pool before
drawing (held against the state snapshot the run started from), and which day
the allocation chained from. A token may change between two consecutive days only
where a fill recorded it as an exception for exactly that pair; a filled day is
also held to "never releases" and to every token having a legitimate source.

`check-state` audits `_state/` itself: the snapshots verify, no day was
numbered outside a session, no token is held by two venues, the counter covers
every number on disk, no commit is half-done, and every run was closed.

`check-lineage` traces each stage back to the one before it: raw payload →
normalized → plugin, plus the digests each manifest recorded for what it read and
wrote. Reports land in `reports_dir` (`check-tokens-<MIC>.{v2,ALL}.txt`, one per
venue; `check-state.{v2,ALL}.txt`).

All three exit non-zero on a hard failure. Soft findings (an unclosed run,
counterTokenV2's expected offset reuse) are reported and do not fail the run.

## Operating notes

- **A Databento download is refused until the day's re-send has happened**
  ("nothing in the live replay was sent on YYYYMMDD"). Wait and run it again.
  OPRA re-sends around 11:30Z in US winter time and lists the day's new
  contracts around 13:00Z; an XCBO download before then is warned about.
- **A venue's raw directory must hold only that day's payload.** A stray file from
  another date is silently blended into the output; `check-lineage` catches this
  as `raw is this day`.
- India (`XBOM`, `XIMC`, and `XNSE` on the Fyers feed) has no historical backfill —
  Fyers serves the current day only, so `normalize --dates --venue XNSE` fails for
  lack of input.

## Known gaps

- `brokerScript1` is an exact copy of `script` for about 20.7% of XCME rows. The
  GLBX parser assumes a single-digit contract year, so families like `RO4G27`
  (root `RO4`, month `G`, year `27`) fall through. Spreads and user-defined combos
  also copy through, but that part is deliberate.
- A re-run overwrites the header's `tokens` block with that run's figures, so a
  day re-normalized after the fact reports `drawn: 0` there. The run log keeps
  every run's own figures, and `check-tokens` derives the day-over-day numbers
  from the allocation tables.
- An older day is normalized with today's `config.ini` and basket lists; they are
  not versioned by date.
- `postgres-normal` (the canonical schema into Postgres) is planned, not built.

## Databases

Throwaway databases for trying the sinks (Docker here is the database, not
premarket):

```bash
docker compose -f docker/contract-clickhouse/docker-compose.yml up -d   # clickhouse-normal: HTTP on 127.0.0.1:8744
docker compose -f docker/contract-postgres/docker-compose.yml up -d     # postgres-plugin-*: 127.0.0.1:6006
```

The Postgres one is `postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable`.
The `.ini.example` files hold placeholders; put real credentials only in your
own copies under `sinks_dir`, which are never committed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
