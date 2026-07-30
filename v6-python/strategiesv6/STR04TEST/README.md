# STR04TEST

Beginning-of-day loader for the STR04TEST strategy -- a copy of STR04 used to
test multi-exchange fetching (equities + non-XNAS symbols like futures) before
folding the same logic back into STR04.

## PRD

- 1-minute OHLCV bars.
- Last three completed XNAS trading sessions (lookback, not forecast --
  Databento's Historical API has no forward-looking data). XNAS governs the
  session calendar for *all* underlyings, even non-XNAS ones.
- Underlyings: one `EXCHANGE|SYMBOL` pair per line in
  [underlying.txt](underlying.txt), e.g. `XNAS|SPY`, `XCBO|ESU6`.
- Output: one CSV per underlying, `data/{SYMBOL}.csv`, overwritten each run.

## Run

From `v6-python/`:

```
python -m strategiesv6 --strategy=str04test
```

Dispatches to `loader.py:main()` via [strategiesv6/\_\_main\_\_.py](../__main__.py).
Running `loader.py` directly also works: `python -m strategiesv6.STR04TEST.loader`.

## Config / secrets

Each line's `EXCHANGE` picks both the dataset and the API key:

| exchange | dataset | key source |
|---|---|---|
| `XNAS` | `EQUS.MINI` | `key_XNAS` |
| `XCBO` | `OPRA.PILLAR` | `key_XCBO` |
| `XCME` | `GLBX.MDP3` | `key_XCME` |

Dataset comes from `premarketv6.sources.databento_src.VENUE_CONFIGS` (shared
with the rest of premarketv6, not duplicated here). Keys come from
`premarketv6.config.load_databento().keys[EXCHANGE]` -- `conf/keys.ini`
`[production]`/`[development]` section (picked via `DATABENTO_ENV`, default
`production`), or a `DATABENTO_KEY_<EXCHANGE>` env var override. An unknown
exchange in `underlying.txt` fails fast at startup, before any fetch.

## Time range

Per session date, fetch window is the full UTC calendar day: `day 00:00 UTC`
to `(day+1) 00:00 UTC` -- **not** RTH-only. Pre-market and after-hours bars
are included (e.g. a `08:50 UTC` bar = 4:50am ET). Three-day period = three
such full-day windows, one per session date from `last_n_trading_sessions`.

## Output schema (data/{SYMBOL}.csv)

| column | meaning |
|---|---|
| `ts` | bar timestamp, UTC |
| `date` | session date, YYYY-MM-DD |
| `underlying` | ticker (bare symbol, no exchange prefix) |
| `open`, `high`, `low`, `close` | bar OHLC |
| `volume` | bar volume |

A ticker with no data on any of the three sessions is skipped (logged), not
fatal -- the run only aborts if *no* underlying produced any rows.

## Notes

- `exchange_calendars` has no `"XNDQ"` calendar -- `"XNAS"` is the correct
  code for Nasdaq-listed names and is what's wired in.
- `loader.py` was originally copied from an options/straddle-matrix script;
  that logic (strike windows, definitions, VT calc) is gone here -- this is
  a plain OHLCV pull, nothing else.
