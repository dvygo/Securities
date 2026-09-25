# premarketv6

Pre-market symbology pipeline, in three stages:

1. **download** each venue's contract master;
2. **normalize** every venue into one schema and assign a stable, cross-venue
   instrument token (`counterTokenV2`) -- files only, never a database;
3. **load** the day into its consumers -- ClickHouse, the Postgres symbol
   master, MDF's token map -- through named sinks.

Design decisions and their reasons are in
[`docs/DESIGN/OLD-DATE-PROCESSING.md`](docs/DESIGN/OLD-DATE-PROCESSING.md).

## Venues and sources

| MIC | Market | Source | Arrives (UTC) | Arrives (IST) |
|-----|--------|--------|---------------|---------------|
| `XCME` | CME Globex futures/options | Databento `GLBX.MDP3` | 00:00–01:00 | 05:30–06:30 |
| `XNAS` | US equities | Databento `EQUS.MINI` | 05:00–06:00 | 10:30–11:30 |
| `XCBO` | US options | Databento `OPRA.PILLAR` | 10:00–11:00 | 15:30–16:30 |
| `XNSE` | NSE India (cash, F&O, currency) | Fyers *or* NSE contract masters — see below | — | — |
| `XBOM` | BSE India | Fyers | — | — |
| `XIMC` | MCX India | Fyers | — | — |

`XNSE` is the one venue two feeds can serve, and `conf/config.ini` picks which:

```ini
[EXCHANGE:XNSE]
feed = fyers   ; Fyers CDN (NSE_CM/FO/CD)      -> XNSE-FYERS.parquet
# feed = nse   ; NSE's own contract masters    -> XNSE-NSE.parquet
```

That value is the whole switch. A normalize step runs a venue only when it owns
it, so the two never both write `XNSE`, and baskets resolve the filename through
the same setting rather than assuming a vendor. Under `feed = fyers` the
`NEW FILE FORMAT/` drop is ignored; under `feed = nse` the Fyers NSE segments are
not downloaded. Switching costs no tokens — the carry-forward keys on the script,
not the source, so a swap keeps every symbol's `counterTokenV2` and draws nothing
new from the shared sequence.

**The venues do not arrive together.** A full day is not available before roughly
**16:30 IST**, because OPRA publishes last. The pipeline is built for this: run
normalize as often as you like, and each run picks up whatever has landed without
disturbing what is already numbered. A common pattern is one run at ~06:30 IST for
XCME and a second at ~16:30 IST for XNAS and XCBO.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
cp conf/config.ini.example conf/config.ini      # venue ids; keys go in conf/keys.ini
for f in conf/sinks/*.ini.example; do cp "$f" "${f%.example}"; done   # then edit

python -m premarketv6 init-state --reason "first run on this host"    # once per host
```

A day -- download, normalize, load:

```bash
python -m premarketv6 xcme --today            # and xnas, xcbo, india
python -m premarketv6 normalize
python -m premarketv6 load --sink clickhouse-normal --sink postgres-plugin-xcme \
                           --sink mdf-tokenmap
```

**Older days** -- history before the first day, or a day that was missed --
download with `--dates`, preview, then fill:

```bash
python -m premarketv6 xcme --dates=20260924,20260923
python -m premarketv6 normalize --dates=20260924,20260923 --venue XCME \
                                --reason "history before go-live" --dry-run
python -m premarketv6 normalize --dates=20260924,20260923 --venue XCME \
                                --reason "history before go-live"
```

A fill runs newest first, takes a backup first, and ends with a quality gate
(check-tokens + check-state). Never `normalize --date-dir` an older date: it is
refused, because numbering it as a live day would fork the token chain.

## Layout of a day

```
data/YYYYMMDD/
  XCME/  glbx-mdp3-YYYYMMDD.definition.dbn.zst      raw vendor payload
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

data/_state/                                       the numbering state (see below)
  manifest.json                                    the head: counter, last run, venues
  <MIC>/<run_id>.alloc.parquet                     state snapshots, never modified
  runs.jsonl                                       the run log (OpenLineage-shaped)
  backups/<run_id>.tar.gz                          before every fill and init-state
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
  "version": 5,
  "date": "20260901", "venue": "XCBO",
  "started_at": "...Z", "completed_at": "...Z",   // when this venue actually ran
  "code":       { "build_sha": "...", "manifest_version": 4 },
  "allocation": { "venue_id": 10, "count": 2007183, "free_count": 69068,
                  "path": "XCBO.alloc.parquet", "sha256": "..." },
  "tokens":     { "arrived": 7937, "departed": 9396, "reused": 7937, "drawn": 0,
                  "sequence_before": 3367460, "sequence_after": 3367460 },
  "inputs":     [{ "path": "XCBO/opra-pillar-...dbn.zst", "sha256": "..." }],
  "outputs":    [{ "path": "TRANSFORM/normalized/...parquet", "rows": 2007183, "sha256": "..." }],
  "numbering":  { "mode": "live", "run_id": "...", "reason": "...", "operator": "user@host",
                  "state_before": { "snapshot": "XCBO/<run>.alloc.parquet", "sha256": "..." },
                  "state_after":  { "snapshot": "XCBO/<run>.alloc.parquet", "sha256": "..." },
                  "neighbours": { "earlier": "20260831" }, "counter_before": 3367460,
                  "counter_after": 3367460, "exceptions": { "count": 0 } }
}
```

Version 5 adds the `numbering` block and a third row state in the allocation
table, `retained`. A version-4 table reads unchanged.

The allocation itself lives in `<MIC>.alloc.parquet` (`script`, `token`, `state`)
rather than inline. Assigned and free share one file deliberately: they are two
halves of one invariant, and splitting them would let a crash leave an allocation
with an empty pool, which the next day would read as "nothing to recycle".

Converting a pre-v4 manifest:

```bash
python -m premarketv6 migrate-manifests --dry-run
python -m premarketv6 migrate-manifests
```

## The numbering state

`data/_state/` is what every numbering run starts from and leaves behind: per
venue, the tokens it holds now and the ones it may hand out. It is created once
per host by `init-state` (from each venue's newest manifest; it refuses if two
venues share a token), and after that `normalize` and `load` refuse to run
without it.

- **One run at a time.** Every numbering or load run holds `data/_state/.lock`; a
  second one exits at once, naming the holder.
- **Crash-safe.** A run reserves its numbers before publishing a file, stages its
  manifests and snapshot, and commits by replacing the head; the next run's
  recovery finishes or discards whatever an interrupted run left. Numbers can
  leak; they are never handed out twice.
- **Audited.** `runs.jsonl` records every run -- START, COMPLETE or FAIL, the
  operator, the reason, what it read and wrote with digests -- in the OpenLineage
  shape, so it can be fed to a lineage catalog.
- **Backed up.** Every fill and `init-state` first writes
  `backups/<run_id>.tar.gz` (every day's manifests and the head; the newest ten
  are kept). Restore from the data root: `tar -xzf _state/backups/<run_id>.tar.gz`.

## Load

`premarketv6 load --date-dir D --sink NAME ...` delivers a normalized day. Each
sink is one `conf/sinks/<name>.ini` (gitignored; copy the `.example`):

| type | what it delivers |
|---|---|
| `clickhouse-normal` | the canonical schema, every venue: `contracts_YYYYMMDD`/`baskets_YYYYMMDD` and the current `contracts`/`baskets` |
| `postgres-plugin` | ONE market, as the legacy 17-column symbol master, upserted on `(token, trade_date)`; its `venue_id` must match config.ini's |
| `mdf-tokenmap` | `tokenmap.<VENUE>.bin` for Databento venues, into the day's tree or MDF's `dir` |

A day older than the newest live day never becomes "current": it loads its dated
ClickHouse tables but not the mirrors, and is refused for MDF's delivery
directory. `--dry-run` validates every sink and connects to nothing. Any setting
can be overridden with `PREMARKET_SINK_<NAME>_<KEY>`.

## Validation

```bash
python -m premarketv6 check-tokens  --dates=20260824,20260825,20260826
python -m premarketv6 check-lineage --dates=20260901 --venue XCBO
python -m premarketv6 check-state
```

`check-tokens` pins the numbering: tokens populated, numeric and inside int32,
one-to-one with scripts, disjoint between venues, covered by the sequence, and
agreeing with the manifest. Across a pair of days it also proves the recycling
actually fired — that departures released, that arrivals drained the pool before
drawing (held against the state snapshot the run started from), and which day
the allocation chained from. A token may change between two consecutive days only
where a fill recorded it as an exception for exactly that pair; a filled day is
also held to "never releases" and to every token having a legitimate source.

`check-state` audits `data/_state/` itself: the snapshots verify, no day was
numbered outside a session, no token is held by two venues, the counter covers
every number on disk, no commit is half-done, and every run was closed.

`check-lineage` traces each stage back to the one before it: raw payload →
normalized → plugin, plus the digests each manifest recorded for what it read and
wrote. Reports land in `docs/QAT_GENERATED/` tagged `[v2]` and `[ALL]`.

Both exit non-zero on a hard failure. Soft findings (a clamped download window,
counterTokenV2's expected offset reuse) are reported and do not fail the run.

## Operating notes

- **Download inside the publish window and you get a partial file.** Databento
  clamps the query to what has published, and the result looks like a complete
  day. `check-lineage` flags it as `full day ... ends HH:MMZ`. Re-download after
  the window closes.
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
