# STR06

Submits a Databento **batch** job for CME's full trades tape, and keeps a record
of every submission so the job can be pulled again later.

## PRD

- Dataset: `GLBX.MDP3` (XCME).
- Schema: `trades`.
- Symbols: `ALL_SYMBOLS` — every instrument CME lists, not a basket.
- Encoding: DBN + zstd, so delivered files are `.dbn.zst`.
- One file per session (`split_duration=day`).
- Default range: one lookback day — the latest **complete** session.
- Download pulls `.dbn` files only, never the JSON sidecars.
- Every submission recorded in [manifest.json](manifest.json).

Batch, not `timeseries.get_range`: the request is queued server-side and
Databento assembles the files, so nothing is streamed or held in memory. For
scale, the 2026-08-21 session priced at 4,673,526 records / 0.22 GB.

## Run

From `v6-python/`:

```
# price the job -- submits nothing
python -m strategiesv6 --strategy=str06

# actually submit
python -m strategiesv6 --strategy=str06 --submit

# a specific session (end is exclusive)
python -m strategiesv6 --strategy=str06 --start 2026-08-20 --end 2026-08-21 --submit

# poll, then fetch the most recent submission
python -m strategiesv6 --strategy=str06 --list
python -m strategiesv6 --strategy=str06 --download

# or a specific job
python -m strategiesv6 --strategy=str06 --download GLBX-20260822-ABCDEF
```

A bare run prints record count, billable size and cost for the exact request
`--submit` would send, then exits. A batch job is billable and cannot be
un-submitted, so the flag is the confirmation step.

## manifest.json

Written next to this file, one entry per submission:

```json
{
  "jobs": [
    {
      "job_id": "GLBX-20260822-ABCDEF",
      "requested_at": "2026-08-22T09:23:24.789363+00:00",
      "request": { "dataset": "GLBX.MDP3", "symbols": "ALL_SYMBOLS", "...": "..." },
      "ack": { "id": "GLBX-20260822-ABCDEF", "state": "queued", "...": "..." },
      "downloads": [
        { "downloaded_at": "...", "files": ["..."] }
      ]
    }
  ]
}
```

The acknowledgement is stored **verbatim**, not picked apart into chosen fields,
so a field Databento adds or renames does not silently vanish from the record.
`request` is the exact argument set sent to `submit_job`, which is what makes a
submission reproducible.

Written atomically (staged, then renamed) — it is the only local record that a
billable job was submitted, and a crash mid-write would otherwise lose every
prior entry along with the new one.

`--list` reads this file rather than `batch.list_jobs()`, refreshing each state
from the API, so a job stays listed after Databento expires it: the record of
what was asked for outlives the data.

## Why downloads are filtered

A finished job carries `metadata.json`, `condition.json` and `symbology.json`
alongside the data. `batch.download()` with no filename fetches the whole job as
one zip and extracts all of it, so STR06 lists the job's files, keeps the ones
ending in `.dbn` (or `.dbn.zst`), and downloads those by name.

Files land in [data/](data/) unless `--output-dir` says otherwise. The SDK nests
them under `{output_dir}/{job_id}/`.

## Compression

Default is `zstd`, giving `.dbn.zst`. `--compression none` asks for literal
`.dbn` instead. The download filter accepts either suffix.

zstd is the default because of what the ranges actually weigh. Billable size is
not file size: the 2026-08-21 session priced at 0.22 GB billable and landed as
**397.7 MB** of plain `.dbn`, about 1.8×. The free rolling year is ~68.8 GB
billable, so uncompressed it is roughly **120 GB across ~250 daily files**. At
that size the transfer dominates.

For a single session either default is fine.

## Config / secrets

`premarketv6.config.load_databento().keys["XCME"]` — `DATABENTO_KEY_XCME` env
var, or `key_XCME` in `conf/keys.ini`, selected by `DATABENTO_ENV`. Same key
`premarketv6`'s xcme downloads use; GLBX.MDP3 is what it is provisioned for.

## Time range

`--end` is **exclusive**, matching the API, so one session is `start=D end=D+1`
— which is the default when neither is given.

The default start is the latest complete session, derived by stepping back one
day from `get_dataset_range()["end"]`. That bound is exclusive and carries a
mid-session timestamp (e.g. `2026-08-21T05:20Z`), so its own date is a partial
session; the same correction `premarketv6`'s definition download makes.

One day rather than a lookback window is deliberate. At ALL_SYMBOLS trades
volume an accidental week is a very expensive typo.

## Dispatcher

`strategiesv6/__main__.py` forwards unrecognised arguments to the loader, so
STR06's flags work through `--strategy=str06`. Running the module directly also
works: `python -m strategiesv6.STR06.loader --list`.
