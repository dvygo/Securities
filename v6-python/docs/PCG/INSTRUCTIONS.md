To: Production Control Group

The pipeline is three stages: download -> normalize -> load. normalize only ever
writes files; everything that reaches a database or MDF goes through `load`.

## Once per host (6.3.0 and later)

1. ```source .venv/bin/activate```
2. Create the sink files: copy each `conf/sinks/*.ini.example` to the same name
   without `.example` and fill in the credentials. One file per destination:
   `clickhouse-normal`, `postgres-plugin-xcme`, `postgres-plugin-xcbo`,
   `postgres-plugin-xnas`, `mdf-tokenmap`. They are gitignored.
3. ```python -m premarketv6 init-state --reason "adopt the numbering state" --dry-run```
   then the same without `--dry-run`. It prints each venue's newest day and the
   counter. normalize and load refuse to run until this has been done.

## Every day

1. ```python -m premarketv6 {india, xnas, xcme, xcbo}```

2. from NSE contract file 'NEW FILE FORMAT.rar', extract entire folder as a
   folder to /{YYYYMMDD}/XNSE/*

   Skip this while conf/config.ini has `[EXCHANGE:XNSE] feed = fyers` --
   step 1's `india` download already supplies XNSE. It applies only when
   XNSE is switched back to the NSE file drop.

3. ```python -m premarketv6 normalize```

   Run it as often as the venues arrive (XCME ~06:30 IST, XNAS and XCBO
   ~16:30 IST). A re-run keeps every token it already gave out.

4. ```python -m premarketv6 load --sink clickhouse-normal --sink postgres-plugin-xcme --sink postgres-plugin-xcbo --sink postgres-plugin-xnas --sink mdf-tokenmap```

   Name only the sinks you want. `--dry-run` checks every sink file and
   connects to nothing. A failed sink does not stop the others, but the
   command exits non-zero -- read the `error:` line.

All output for the day lands under data/{YYYYMMDD}/TRANSFORM/.

## Filling an older day

For history before a host's first day, or a day that was missed:

1. ```python -m premarketv6 xcme --dates=YYYYMMDD[,YYYYMMDD...]```   (and xnas / xcbo)
2. ```python -m premarketv6 normalize --dates=... --venue XCME --reason "why" --dry-run```
   -- read the preview: per date and venue, where the tokens come from, how
   many are new, and how many exceptions.
3. The same without `--dry-run`. It takes a backup first, fills newest first,
   and ends with a quality gate. If the gate fails, the filled days are on disk
   but must not be loaded until it is resolved.
4. `load` the filled days only if a consumer needs them. A day older than the
   newest live day never replaces the current ClickHouse tables or MDF's map.

Never `normalize --date-dir` an older day -- it is refused on purpose.
India (XBOM, XIMC, XNSE via Fyers) cannot be filled: Fyers serves today only.

## When something refuses

- **"another numbering run holds ... .lock"** -- a normalize/load is already
  running (the message names it). Wait for it; do not delete the lock.
- **"no state head ... run init-state"** -- this host was never initialised.
- **"older than ..., which is already numbered"** -- use `normalize --dates`.
- **"refusing to deliver ... map"** -- that day is not the newest live day;
  MDF would load an old map as current.
- **check-state / check-tokens FAIL** -- stop and escalate; do not re-run
  blindly.

## Checks

```python -m premarketv6 check-state```
```python -m premarketv6 check-tokens --dates=YYYYMMDD,YYYYMMDD```

Reports land in docs/QAT_GENERATED/. The run log is data/_state/runs.jsonl.

## Restoring

Every fill and init-state writes data/_state/backups/<run_id>.tar.gz first (the
newest ten are kept). To restore, from the data directory:

```tar -xzf _state/backups/<run_id>.tar.gz```

then run check-state.
