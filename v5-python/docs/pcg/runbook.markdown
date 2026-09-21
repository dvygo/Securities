# Premarket v5 — Daily Runbook

Run this every morning before market open. Setup must be done first (see `docs/deploy/setup.markdown`).

## 0. Get ready

```
cd Securities/v5-python
source .venv/bin/activate
```

## 1. Download India

```
python -m premarket india
```

This pulls the Fyers symbol files for NSE, BSE and MCX into `data/YYYYMMDD/raw/FYERS/`.

## 2. Download the US venues (if needed today)

```
python -m premarket xnas
python -m premarket xcme
python -m premarket xcbo
```

Run only the ones you need. These use the Databento keys in `conf/keys.ini`.

## 3. Normalize

```
python -m premarket normalize
```

This turns everything downloaded today into one common format under `data/YYYYMMDD/normalized/` and rebuilds the baskets.

## 4. Push to Postgres

```
python -m premarket normalize --only postgres
```

This loads the normalized data into the contract DB set in `conf/config.ini`:

- `v4_YYYYMMDD.contracts` and `v4_YYYYMMDD.baskets`
- `v4_YYYYMMDD_baskets.baskets`
- `public.contracts` and `public.baskets` (always today's copy)

Look for this line at the end:

```
Successfully pushed to v4_YYYYMMDD, v4_YYYYMMDD_baskets, public
```

If you see `Error: No database URL found` instead, the push did not happen, even though the command finished. Fix `[postgres]` in `conf/config.ini` and run step 4 again.

**Shortcut:** steps 3 and 4 together:

```
python -m premarket normalize --postgres-push
```

## 5. Plugin push (only if asked)

```
python -m premarket normalize --plugin
```

This builds the plugin CSVs in `data/YYYYMMDD/plugin/` and appends them to the `[postgres-plugin]` DB. It only pushes the exchanges listed in `exchanges =`.

## Check it landed

In psql against the contract DB:

```
select exchange, count(*) from v4_YYYYMMDD.contracts group by exchange;
```

Each venue you downloaded should show a non-zero count.

## Rerunning

- Reruns are safe. The push drops and recreates the tables each time.
- To redo a past day, add `--date-dir YYYYMMDD` to any command:

  ```
  python -m premarket normalize --date-dir 20260918 --postgres-push
  ```

- To dry-run without writing anything, add `--dry-run`.

## When something fails

- Every run writes a log to `bin/LOGS/`. The path is printed at the top of the run as `Log: ...`.
- `ModuleNotFoundError`: the venv isn't active. Run `source .venv/bin/activate`.
- `Could not find v5-python repo root`: you're not in the `v5-python` folder. `cd` there.
- Postgres connection errors: run the connection test from `docs/deploy/setup.markdown` step 8.
