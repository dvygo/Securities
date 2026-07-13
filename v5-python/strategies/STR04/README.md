# STR04

Beginning-of-day loader for the STR04 real-time strategy.

## PRD

- 1-minute OHLCV bars.
- Last three completed XNAS trading sessions (lookback, not forecast --
  Databento's Historical API has no forward-looking data).
- Underlyings: one ticker per line in [underlying.txt](underlying.txt).
- Output: single flat [str04.csv](str04.csv), overwritten each run.

## Run

From `v5-python/`:

```
python -m strategies --strategy=str04
```

Dispatches to `loader.py:main()` via [strategies/\_\_main\_\_.py](../__main__.py).
Running `loader.py` directly also works: `python -m strategies.STR04.loader`.

## Config / secrets

Uses `premarket.config.load_databento().api_key` -- same key already used by
`opra_pillar.py` / `equs_mini.py` ("Key 2" in `conf/config.ini`, or the
`DATABENTO_API_KEY` env var). No separate secret for STR04.

Dataset: `EQUS.MINI` (what that key is provisioned for). Trading-session
calendar: `exchange_calendars` `"XNAS"` -- used only to figure out which
calendar days are sessions, independent of the dataset name.

## Time range

Per session date, fetch window is the full UTC calendar day: `day 00:00 UTC`
to `(day+1) 00:00 UTC` -- **not** RTH-only. Pre-market and after-hours bars
are included (e.g. a `08:50 UTC` bar = 4:50am ET). Three-day period = three
such full-day windows, one per session date from `last_n_trading_sessions`.

## Output schema (str04.csv)

| column | meaning |
|---|---|
| `ts` | bar timestamp, UTC |
| `date` | session date, YYYY-MM-DD |
| `underlying` | ticker |
| `open`, `high`, `low`, `close` | bar OHLC |
| `volume` | bar volume |

## Notes

- `exchange_calendars` has no `"XNDQ"` calendar -- `"XNAS"` is the correct
  code for Nasdaq-listed names and is what's wired in.
- `loader.py` was originally copied from an options/straddle-matrix script;
  that logic (strike windows, definitions, VT calc) is gone here -- this is
  a plain OHLCV pull, nothing else.
