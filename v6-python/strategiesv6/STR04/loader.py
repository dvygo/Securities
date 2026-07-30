"""STR04 beginning-of-day loader: python -m strategiesv6 --strategy=str04

Fetches 1-minute OHLCV bars for every underlying in underlying.txt, covering
the last three completed XNAS trading sessions, and writes one CSV per
underlying to data/{underlying}.csv. "Three trading days" means the three
most recently closed sessions as of run time -- Databento's Historical API
has no forward-looking data, so this is a lookback, not a forecast.

Uses premarketv6.config's XNAS key (DATABENTO_KEY_XNAS env var, or conf/keys.ini
[production]/[development] key_XNAS, selected via DATABENTO_ENV). Dataset is
EQUS.MINI, which is what that key is provisioned for; EXCHANGE_CAL="XNAS" is
only the trading-session calendar, independent of dataset choice.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import exchange_calendars as xcals
import pandas as pd

from premarketv6 import config as premarket_config

EXCHANGE_CAL = "XNAS"
DATASET = "EQUS.MINI"
LOOKBACK_TRADING_DAYS = 3

STRATEGY_DIR = Path(__file__).resolve().parent
UNDERLYINGS_FILE = STRATEGY_DIR / "underlying.txt"
DATA_DIR = STRATEGY_DIR / "data"

UTC = ZoneInfo("UTC")

OUT_COLUMNS = ["ts", "date", "underlying", "open", "high", "low", "close", "volume"]


# ------------------------------------------------------------
# CALENDAR
# ------------------------------------------------------------
def utc_today() -> date:
    return datetime.now(UTC).date()


def latest_session_on_or_before(cal, d: date) -> date:
    """Latest XNAS session with session date <= d (weekends/holidays snap back)."""
    ts = pd.Timestamp(d)
    if cal.is_session(ts):
        return d
    window_start = d - timedelta(days=30)
    sessions = cal.sessions_in_range(pd.Timestamp(window_start), ts)
    if len(sessions) == 0:
        raise SystemExit(f"No {EXCHANGE_CAL} session on or before {d.isoformat()}")
    return sessions[-1].date()


def last_n_trading_sessions(cal, n: int) -> list[date]:
    """Oldest-first list of the last n XNAS sessions strictly before today (today's
    session is excluded -- this runs before the close, so today isn't done yet)."""
    end = latest_session_on_or_before(cal, utc_today() - timedelta(days=1))
    ts = pd.Timestamp(end)
    out: list[date] = []
    for _ in range(n):
        out.append(ts.date())
        ts = cal.previous_session(ts)
    return list(reversed(out))


# ------------------------------------------------------------
# DATA
# ------------------------------------------------------------
def load_underlyings(path: Path) -> list[str]:
    with open(path, "r") as f:
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def fetch_ohlcv_1m(hist: db.Historical, underlying: str, day: date) -> pd.DataFrame:
    df = hist.timeseries.get_range(
        dataset=DATASET,
        schema="ohlcv-1m",
        symbols=[underlying],
        start=day,
        end=day + timedelta(days=1),
    ).to_df()

    if df.empty:
        return df

    ts = pd.to_datetime(df["ts_event"], utc=True) if "ts_event" in df.columns else pd.to_datetime(df.index, utc=True)
    return pd.DataFrame({
        "ts": ts,
        "date": day.isoformat(),
        "underlying": underlying,
        "open": df["open"].values,
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "volume": df["volume"].values,
    }).sort_values("ts")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main() -> int:
    cfg = premarket_config.load_databento()
    api_key = cfg.keys.get("XNAS", "")
    if not api_key:
        raise SystemExit(
            "Missing Databento key: set DATABENTO_KEY_XNAS env var, or "
            "key_XNAS in conf/keys.ini."
        )
    hist = db.Historical(api_key)

    underlyings = load_underlyings(UNDERLYINGS_FILE)
    if not underlyings:
        raise SystemExit(f"No underlyings found in {UNDERLYINGS_FILE}")

    cal = xcals.get_calendar(EXCHANGE_CAL)
    sessions = last_n_trading_sessions(cal, LOOKBACK_TRADING_DAYS)
    print(
        f"STR04: {len(underlyings)} underlyings x {len(sessions)} sessions "
        f"({sessions[0].isoformat()}..{sessions[-1].isoformat()})",
        flush=True,
    )

    DATA_DIR.mkdir(exist_ok=True)

    total_rows = 0
    wrote_any = False
    for underlying in underlyings:
        frames = []
        for day in sessions:
            print(f"  fetch {underlying} {day.isoformat()}", flush=True)
            df = fetch_ohlcv_1m(hist, underlying, day)
            if df.empty:
                print("    no data", flush=True)
                continue
            frames.append(df)

        if not frames:
            print(f"  SKIP {underlying}: no data for any session", flush=True)
            continue

        out_csv = DATA_DIR / f"{underlying}.csv"
        result = pd.concat(frames, ignore_index=True)[OUT_COLUMNS].sort_values("ts")
        result.to_csv(out_csv, index=False)
        print(f"  wrote {len(result)} rows -> {out_csv}", flush=True)
        total_rows += len(result)
        wrote_any = True

    if not wrote_any:
        raise SystemExit("No OHLCV data fetched for any underlying/session")

    print(f"DONE: wrote {total_rows} rows across {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
