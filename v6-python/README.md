# premarketv6

Pre-market symbology pipeline. Downloads each venue's contract master, normalizes
every venue into one schema, assigns a stable cross-venue instrument token, and
builds the plugin Parquet the downstream symbol-master consumes.

## Venues and sources

| MIC | Market | Source | Arrives (UTC) | Arrives (IST) |
|-----|--------|--------|---------------|---------------|
| `XCME` | CME Globex futures/options | Databento `GLBX.MDP3` | 00:00–01:00 | 05:30–06:30 |
| `XNAS` | US equities | Databento `EQUS.MINI` | 05:00–06:00 | 10:30–11:30 |
| `XCBO` | US options | Databento `OPRA.PILLAR` | 10:00–11:00 | 15:30–16:30 |
| `XNSE` | NSE India (cash, F&O, currency) | NSE contract masters, dropped by the broker | — | — |
| `XBOM` | BSE India | Fyers | — | — |
| `XIMC` | MCX India | Fyers | — | — |

**The venues do not arrive together.** A full day is not available before roughly
**16:30 IST**, because OPRA publishes last. The pipeline is built for this: run
normalize as often as you like, and each run picks up whatever has landed without
disturbing what is already numbered. A common pattern is one run at ~06:30 IST for
XCME and a second at ~16:30 IST for XNAS and XCBO.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
cp conf/config.ini.example conf/config.ini      # then set [databento] api_key, venue ids
```

Download, then normalize:

```bash
python -m premarketv6 xcme --all-symbols --today
python -m premarketv6 xnas --all-symbols --today
python -m premarketv6 xcbo --all-symbols --today
python -m premarketv6 india

python -m premarketv6 normalize --plugin --csv-only
```

`--csv-only` writes files and never touches a database. Drop it only when you
intend to push.

Backfill a specific set of dates (one batch job per date, all submitted before
any is waited on):

```bash
python -m premarketv6 xcbo --all-symbols --dates=20260901,20260831
python -m premarketv6 normalize --date-dir=20260901 --plugin --csv-only
```

## Layout of a day

```
data/YYYYMMDD/
  XCME/  glbx-mdp3-YYYYMMDD.definition.dbn.zst      raw vendor payload
  XNAS/  equs-mini-YYYYMMDD.definition.dbn.zst
  XCBO/  opra-pillar-YYYYMMDD.definition.dbn.zst
  XNSE/  NEW FILE FORMAT/                            NSE contract masters
  v6/
    normalized/   <MIC>-<SOURCE>.parquet             one file per venue
    plugin/       <MIC>-<SOURCE>.parquet             legacy symbol-master shape
    manifests/    <MIC>.json                         header: the completion record
                  <MIC>.alloc.parquet                the venue's token allocation
                  _sequence.json                     the day's shared counter
```

## counterTokenV2

One integer sequence, shared by every venue, starting at 1. `scriptToken` carries
each source's own instrument id, which is only unique within that source — on
2026-08-12 the raw ids collided 932 times between XCME and XNAS. Anything keying
on a token without an exchange column needs something collision-free.

Three rules, applied per venue against the previous day:

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

**Re-running a day is safe.** The allocation is anchored on the day's own manifest
when one exists, so a second pass keeps every token it already issued and only
numbers what is genuinely new. Re-normalizing the full week moved zero tokens
across 30 venue-days.

## Manifests

A venue's header is written only after its normalized Parquet is promoted, so
**its presence is the answer to "is this venue done for this date"**. Absent means
not done yet; it never means empty.

```jsonc
{
  "version": 4,
  "date": "20260901", "venue": "XCBO",
  "started_at": "...Z", "completed_at": "...Z",   // when this venue actually ran
  "code":       { "build_sha": "...", "manifest_version": 4 },
  "allocation": { "venue_id": 10, "count": 2007183, "free_count": 69068,
                  "path": "XCBO.alloc.parquet", "sha256": "..." },
  "tokens":     { "arrived": 7937, "departed": 9396, "reused": 7937, "drawn": 0,
                  "sequence_before": 3367460, "sequence_after": 3367460 },
  "inputs":     [{ "path": "XCBO/opra-pillar-...dbn.zst", "sha256": "..." }],
  "outputs":    [{ "path": "v6/normalized/...parquet", "rows": 2007183, "sha256": "..." }]
}
```

The allocation itself lives in `<MIC>.alloc.parquet` (`script`, `token`, `state`)
rather than inline. Assigned and free share one file deliberately: they are two
halves of one invariant, and splitting them would let a crash leave an allocation
with an empty pool, which the next day would read as "nothing to recycle".

Converting a pre-v4 manifest:

```bash
python -m premarketv6 migrate-manifests --dry-run
python -m premarketv6 migrate-manifests
```

## Validation

```bash
python -m premarketv6 check-tokens  --dates=20260824,20260825,20260826
python -m premarketv6 check-lineage --dates=20260901 --venue XCBO
```

`check-tokens` pins the numbering: tokens populated, numeric and inside int32,
one-to-one with scripts, disjoint between venues, covered by the sequence, and
agreeing with the manifest. Across a pair of days it also proves the recycling
actually fired — that departures released, that arrivals drained the pool before
drawing, and which day the allocation chained from.

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
- India (`XBOM`, `XIMC`) has no historical backfill — Fyers serves the current day
  only.

## Known gaps

- `brokerScript1` is an exact copy of `script` for about 20.7% of XCME rows. The
  GLBX parser assumes a single-digit contract year, so families like `RO4G27`
  (root `RO4`, month `G`, year `27`) fall through. Spreads and user-defined combos
  also copy through, but that part is deliberate.
- A re-run overwrites the `tokens` block with that run's figures, so a day
  re-normalized after the fact reports `drawn: 0` rather than what the original
  numbering did. The allocation is unaffected, and `check-tokens` still derives
  the real day-over-day figures from the two allocation tables.
