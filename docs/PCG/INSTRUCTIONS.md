To: Production Control Group

The pipeline is three stages: download -> normalize -> load. normalize only ever
writes files; everything that reaches a database or MDF goes through `load`.

Every command is the one program, with the config and the keys by full path:

```PM="/opt/securities/current/bin/premarket --config=/opt/securities/conf/config.ini --keys=/opt/securities/conf/keys.ini"```

## Once per host (8.0.0 and later)

1. Install the release: deploy/SETUP.md, steps 1-2.
2. Create the sink files: copy each `/opt/securities/conf/sinks/*.ini.example`
   to the same name without `.example` and fill in the credentials. One file per
   destination: `clickhouse-normal`, `postgres-plugin-xcme`,
   `postgres-plugin-xcbo`, `postgres-plugin-xnas`, `mdf-tokenmap`.
3. ```$PM init-state --reason "adopt the numbering state" --dry-run```
   then the same without `--dry-run`. It prints each venue's newest day and the
   counter. normalize and load refuse to run until this has been done.

   A host that ran 7.x does not run init-state: it runs
   ```$PM migrate-state --reason "upgrade to 8.0.0"``` once instead
   (deploy/SETUP.md, "Upgrade from 7.x").

## Every day

1. ```$PM india```, ```$PM xnas```, ```$PM xcme```, ```$PM xcbo```

2. from NSE contract file 'NEW FILE FORMAT.rar', extract entire folder as a
   folder to /{YYYYMMDD}/XNSE/*

   Skip this while config.ini has `[venue.XNSE] feed = fyers` -- step 1's
   `india` download already supplies XNSE. It applies only when XNSE is
   switched back to the NSE file drop.

3. ```$PM normalize --reason "daily"```

   Run it as often as the venues arrive. A re-run keeps every token it already
   gave out.

4. ```$PM load```

   Every sink in `/opt/securities/conf/sinks/`, or name them with `--sink NAME`.
   `--dry-run` checks every sink file and connects to nothing. A failed sink
   does not stop the others, but the command exits non-zero -- read the
   `error:` line.

All output for the day lands under state/data/{YYYYMMDD}/TRANSFORM/.

## Filling an older day

For history before a host's first day, or a day that was missed:

1. Put the day's definition file (live instrument ids) in
   state/data/{YYYYMMDD}/{MIC}/ by hand: a download is today's live replay only.
2. ```$PM normalize --dates=... --venue XCME --reason "why" --dry-run```
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
- **"is a 7.x numbering state ... Run `premarket migrate-state`"** -- the host
  was upgraded and not migrated yet.
- **"older than ..., which is already numbered"** -- use `normalize --dates`.
- **"refusing to deliver ... map"** -- that day is not the newest live day;
  MDF would load an old map as current.
- **"... is not a known key"** -- a typo in the file and line it names.
- **check-state / check-tokens FAIL** -- stop and escalate; do not re-run
  blindly.

## Checks

```$PM check-state```
```$PM check-tokens --dates=YYYYMMDD,YYYYMMDD```

Reports land in state/reports/. The run log is state/data/_state/runs.jsonl.

## Restoring

Every fill, init-state and migrate-state writes
state/data/_state/backups/<run_id>.tar.gz first (the newest ten are kept). To
restore, from the data directory:

```tar -xzf _state/backups/<run_id>.tar.gz```

then run check-state.
