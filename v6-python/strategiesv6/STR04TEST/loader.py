"""STR04TEST beginning-of-day loader: python -m strategiesv6 --strategy=str04test

Fetches 1-minute OHLCV bars for every underlying in underlying.txt (each line
"EXCHANGE|SYMBOL", e.g. "XNAS|SPY" or "XCBO|ESU6"), covering the last three
completed XNAS trading sessions, and writes one CSV per underlying to
data/{underlying}.csv. "Three trading days" means the three most recently
closed XNAS sessions as of run time -- Databento's Historical API has no
forward-looking data, so this is a lookback, not a forecast.

Per-line exchange picks both the Databento dataset and API key: dataset via
premarketv6.sources.databento_src.VENUE_CONFIGS (XNAS->EQUS.MINI,
XCBO->OPRA.PILLAR, XCME->GLBX.MDP3), key via premarketv6.config's per-exchange
keys (DATABENTO_KEY_<EXCHANGE> env var, or conf/keys.ini key_<EXCHANGE>,
selected via DATABENTO_ENV).
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import exchange_calendars as xcals
import pandas as pd

from premarketv6 import config as premarket_config
from premarketv6.sources.databento_src import VENUE_CONFIGS

EXCHANGE_CAL = "XNAS"
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
def load_underlyings(path: Path) -> list[tuple[str, str]]:
    """Parse 'EXCHANGE|SYMBOL' lines into (exchange, symbol) pairs, uppercased."""
    out: list[tuple[str, str]] = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            exchange, sep, symbol = line.partition("|")
            if not sep or not symbol:
                raise SystemExit(f"Bad underlying line (want EXCHANGE|SYMBOL): {line!r}")
            out.append((exchange.strip().upper(), symbol.strip().upper()))
    return out


def fetch_ohlcv_1m(hist: db.Historical, dataset: str, underlying: str, day: date) -> pd.DataFrame:
    df = hist.timeseries.get_range(
        dataset=dataset,
        schema="ohlcv-1m",
        symbols=[underlying],
        start=day
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

    underlyings = load_underlyings(UNDERLYINGS_FILE)
    if not underlyings:
        raise SystemExit(f"No underlyings found in {UNDERLYINGS_FILE}")

    known_exchanges = {venue.venue_name for venue in VENUE_CONFIGS.values()}
    unknown = sorted({exchange for exchange, _ in underlyings} - known_exchanges)
    if unknown:
        raise SystemExit(f"Unknown exchange(s) {unknown} in {UNDERLYINGS_FILE} (want one of {sorted(known_exchanges)})")

    clients: dict[str, db.Historical] = {}

    def get_client(exchange: str) -> db.Historical:
        if exchange not in clients:
            api_key = cfg.keys.get(exchange, "")
            if not api_key:
                raise SystemExit(
                    f"Missing Databento key for {exchange}: set DATABENTO_KEY_{exchange} env var, "
                    f"or key_{exchange} in conf/keys.ini."
                )
            clients[exchange] = db.Historical(api_key)
        return clients[exchange]

    cal = xcals.get_calendar(EXCHANGE_CAL)
    sessions = last_n_trading_sessions(cal, LOOKBACK_TRADING_DAYS)
    print(
        f"STR04TEST: {len(underlyings)} underlyings x {len(sessions)} sessions "
        f"({sessions[0].isoformat()}..{sessions[-1].isoformat()})",
        flush=True,
    )

    DATA_DIR.mkdir(exist_ok=True)

    total_rows = 0
    wrote_any = False
    for exchange, underlying in underlyings:
        venue_cfg = next(v for v in VENUE_CONFIGS.values() if v.venue_name == exchange)
        hist = get_client(exchange)

        frames = []
        for day in sessions:
            print(f"  fetch {exchange}|{underlying} {day.isoformat()}", flush=True)
            df = fetch_ohlcv_1m(hist, venue_cfg.dataset, underlying, day)
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
