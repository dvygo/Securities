# pip install databento pandas numpy pyarrow exchange-calendars

import configparser
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import databento as db

# ============================================================
# USER CONFIG
# ============================================================
STRIKES_PADDING = 10

DATASET_STOCK = "XNAS.ITCH"
DATASET_OPT   = "OPRA.PILLAR"
EXCHANGE_CAL  = "XNAS"

UNDERLYINGS_FILE = "underlying.txt"
BASE_OUT_DIR = Path("output_matrix")
CONFIG_INI = Path(__file__).resolve().parent / "config.ini"
# ============================================================

UTC = ZoneInfo("UTC")


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def utc_today() -> date:
    return datetime.now(UTC).date()


def parse_yyyymmdd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y%m%d").date()


def fmt_yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _session_ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d)


def is_session(cal, d: date) -> bool:
    return bool(cal.is_session(_session_ts(d)))


def latest_session_on_or_before(cal, d: date) -> date:
    """Latest XNAS session with session date <= d (weekends/holidays snap back)."""
    ts = _session_ts(d)
    if cal.is_session(ts):
        return d
    window_start = d - timedelta(days=30)
    sessions = cal.sessions_in_range(_session_ts(window_start), ts)
    if len(sessions) == 0:
        raise SystemExit(
            f"No {EXCHANGE_CAL} session on or before {fmt_yyyymmdd(d)}"
        )
    return sessions[-1].date()


def resolve_end_date(today: date, cal) -> date:
    """Latest XNAS session strictly before ``today`` (usually yesterday)."""
    return latest_session_on_or_before(cal, today - timedelta(days=1))


def last_n_trading_sessions(cal, end: date, n: int) -> list[date]:
    """Newest-first list of the last n XNAS sessions on or before end."""
    if n < 1:
        raise ValueError("lookback_trading_days must be >= 1")
    end = latest_session_on_or_before(cal, end)
    ts = _session_ts(end)
    out: list[date] = []
    for _ in range(n):
        out.append(ts.date())
        ts = cal.previous_session(ts)
    return out


def calendar_week_friday(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7)


def dte_for_day(d: date) -> int:
    return (calendar_week_friday(d) - d).days


def adjusted_week_expiry(cal, week_monday: date) -> date | None:
    fri = week_monday + timedelta(days=4)
    for delta in range(5):
        cand = fri - timedelta(days=delta)
        if cand < week_monday:
            break
        if is_session(cal, cand):
            return cand
    return None


def target_expiry_for_day(cal, day: date) -> date | None:
    week_mon = day - timedelta(days=day.weekday())
    return adjusted_week_expiry(cal, week_mon)


def load_underlyings(path: str) -> list[str]:
    with open(path, "r") as f:
        return [
            line.strip().upper()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def minutes_to_expiry(expiry_date, ts):
    expiry_dt = pd.Timestamp(
        year=expiry_date.year,
        month=expiry_date.month,
        day=expiry_date.day,
        hour=21,
        minute=0,
        tz="UTC",
    )
    mins = (expiry_dt - ts).total_seconds() / 60.0
    return max(mins, 1.0)


def _read_config() -> configparser.ConfigParser:
    if not CONFIG_INI.is_file():
        raise SystemExit(f"Missing {CONFIG_INI}")
    cp = configparser.ConfigParser()
    cp.read(CONFIG_INI, encoding="utf-8")
    return cp


def get_databento_key() -> str:
    key = _read_config().get("databento", "api_key", fallback="").strip()
    if not key:
        raise SystemExit(f"Missing [databento] api_key in {CONFIG_INI}")
    return key


def get_lookback_trading_days() -> int:
    raw = _read_config().get(
        "histwithvt", "lookback_trading_days", fallback="10"
    ).strip()
    try:
        n = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid lookback_trading_days in {CONFIG_INI}: {raw!r}"
        )
    if n < 1:
        raise SystemExit(f"lookback_trading_days must be >= 1, got {n}")
    return n


def get_end_date_config_raw() -> str:
    return _read_config().get("histwithvt", "end_date", fallback="*").strip()


def resolve_end_date_from_config(cal) -> tuple[date, str, str]:
    """
    Return (end_session_date, config_token, note).

    *          -> latest XNAS session before UTC today (yesterday, holidays snap back)
    YYYYMMDD   -> latest XNAS session on or before that **calendar** day (weekends/holidays snap back)
    """
    raw = get_end_date_config_raw()
    if raw == "*":
        today = utc_today()
        anchor = today - timedelta(days=1)
        end = resolve_end_date(today, cal)
        note = ""
        if end != anchor:
            note = (
                f"* mode: {fmt_yyyymmdd(anchor)} ({anchor.strftime('%A')}) is not an "
                f"{EXCHANGE_CAL} session; using {fmt_yyyymmdd(end)}"
            )
        return end, "*", note
    if len(raw) == 8 and raw.isdigit():
        anchor = parse_yyyymmdd(raw)
        end = latest_session_on_or_before(cal, anchor)
        note = ""
        if end != anchor:
            note = (
                f"end_date {raw} ({anchor.strftime('%A')}) is not an {EXCHANGE_CAL} "
                f"session; using prior session {fmt_yyyymmdd(end)}"
            )
        return end, raw, note
    raise SystemExit(
        f"Invalid end_date in {CONFIG_INI}: {raw!r} "
        "(use * or YYYYMMDD)"
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    key = get_databento_key()
    hist = db.Historical(key)

    underlyings = load_underlyings(UNDERLYINGS_FILE)
    if not underlyings:
        raise SystemExit("No underlyings found")

    cal = xcals.get_calendar(EXCHANGE_CAL)
    end, end_cfg, end_note = resolve_end_date_from_config(cal)
    lookback = get_lookback_trading_days()
    sessions = last_n_trading_sessions(cal, end, lookback)

    print(
        f"utc_today={fmt_yyyymmdd(utc_today())} end_date_cfg={end_cfg} "
        f"END_DATE={fmt_yyyymmdd(end)} lookback={lookback} "
        f"trading_sessions={len(sessions)}",
        flush=True,
    )
    if end_note:
        print(f"  note: {end_note}", flush=True)
    if sessions:
        print(
            f"  newest={fmt_yyyymmdd(sessions[0])} "
            f"oldest={fmt_yyyymmdd(sessions[-1])}",
            flush=True,
        )
        if end_cfg != "*" and sessions[0] != end:
            print(
                f"  warning: newest session != END_DATE (unexpected)",
                flush=True,
            )

    progress: list[str | None] = [None]
    try:
        _run_downloads(hist, cal, underlyings, sessions, progress)
    except KeyboardInterrupt:
        where = progress[0]
        if where:
            print(
                f"\nInterrupted (Ctrl+C) at {where}. "
                "Completed CSVs were kept.",
                flush=True,
            )
        else:
            print("\nInterrupted (Ctrl+C) before processing.", flush=True)
        raise SystemExit(130) from None

    print("\nDONE.")


def _run_downloads(hist, cal, underlyings, sessions, current_holder: list):
    for UNDERLYING in underlyings:
        print(f"\n############################")
        print(f"### PROCESSING {UNDERLYING}")
        print(f"############################")

        for day in sessions:
            current_holder[0] = f"{UNDERLYING} {fmt_yyyymmdd(day)}"
            dte = dte_for_day(day)
            target_exp = target_expiry_for_day(cal, day)
            if target_exp is None:
                print(f"skip {UNDERLYING} {fmt_yyyymmdd(day)}: no XNAS week expiry")
                continue

            print(
                f"\n=== {UNDERLYING} | {fmt_yyyymmdd(day)} | "
                f"DTE={dte} | exp={fmt_yyyymmdd(target_exp)} ==="
            )

            # --------------------------------------------------
            # 1) STOCK
            # --------------------------------------------------
            stk = hist.timeseries.get_range(
                dataset=DATASET_STOCK,
                schema="ohlcv-1m",
                symbols=[UNDERLYING],
                start=day,
                end=day + timedelta(days=1),
            ).to_df()

            if stk.empty:
                continue

            if "ts_event" in stk.columns:
                stk["ts"] = pd.to_datetime(stk["ts_event"], utc=True)
            elif "ts" in stk.columns:
                stk["ts"] = pd.to_datetime(stk["ts"], utc=True)
            else:
                stk["ts"] = stk.index

            stk = (
                stk.sort_values("ts")[["ts", "close"]]
                .rename(columns={"close": "underlying_ltp"})
                .set_index("ts")
            )

            spot = stk["underlying_ltp"].iloc[-1]
            print(f"Underlying spot: {spot:.2f}")

            # --------------------------------------------------
            # 2) DEFINITIONS
            # --------------------------------------------------
            defs = hist.timeseries.get_range(
                dataset=DATASET_OPT,
                schema="definition",
                symbols=f"{UNDERLYING}.OPT",
                stype_in="parent",
                start=day,
            ).to_df()

            if defs.empty:
                continue

            defs["expiration"] = pd.to_datetime(defs["expiration"], utc=True)
            defs["exp_date"] = defs["expiration"].dt.date
            defs = defs[defs["exp_date"] == target_exp]

            if defs.empty:
                print(f"skip {UNDERLYING} {fmt_yyyymmdd(day)}: no defs for {target_exp}")
                continue

            defs["strike"] = defs["strike_price"].astype(float)
            defs["cp"] = defs["raw_symbol"].str.extract(r"([CP])(?=\d+$)")[0]
            defs = defs.dropna(subset=["strike", "cp"])

            # --------------------------------------------------
            # 3) STRIKE WINDOW
            # --------------------------------------------------
            strikes = np.array(sorted(defs["strike"].unique()))
            atm_idx = np.argmin(np.abs(strikes - spot))

            lo = max(0, atm_idx - STRIKES_PADDING)
            hi = min(len(strikes), atm_idx + STRIKES_PADDING + 1)

            keep_strikes = set(strikes[lo:hi])
            defs = defs[defs["strike"].isin(keep_strikes)]
            symbols = defs["raw_symbol"].tolist()

            # --------------------------------------------------
            # 4) OPTION DATA
            # --------------------------------------------------
            opt = hist.timeseries.get_range(
                dataset=DATASET_OPT,
                schema="ohlcv-1m",
                symbols=symbols,
                stype_in="raw_symbol",
                start=day,
            ).to_df()

            if opt.empty:
                continue

            if "ts_event" in opt.columns:
                opt["ts"] = pd.to_datetime(opt["ts_event"], utc=True)
            else:
                opt["ts"] = opt.index

            opt = (
                opt.sort_values("ts")[["ts", "symbol", "close"]]
                .rename(columns={"close": "mid"})
            )

            # --------------------------------------------------
            # 5) MATRIX
            # --------------------------------------------------
            matrix = stk.copy()

            for sym, g in opt.groupby("symbol"):
                s = (
                    g.set_index("ts")["mid"]
                    .resample("1min")
                    .last()
                    .ffill()
                )
                matrix[sym] = s

            matrix = matrix.dropna(subset=["underlying_ltp"])

            # --------------------------------------------------
            # 6) STRADDLE + VT
            # --------------------------------------------------
            meta = defs.set_index("raw_symbol")[["strike", "cp"]]
            calls = meta[meta["cp"] == "C"]
            puts  = meta[meta["cp"] == "P"]

            common_strikes = np.intersect1d(
                calls["strike"], puts["strike"]
            )
            strikes_arr = np.array(sorted(common_strikes))

            def calc_straddle_vt(row):

                ltp = row["underlying_ltp"]
                ts = row.name

                nearest = strikes_arr[
                    np.argsort(np.abs(strikes_arr - ltp))[:3]
                ]

                mins = []

                for k in nearest:

                    c_sym = calls[calls["strike"] == k].index[0]
                    p_sym = puts[puts["strike"] == k].index[0]

                    c = row.get(c_sym)
                    p = row.get(p_sym)

                    if pd.notna(c) and pd.notna(p):
                        mins.append(2 * min(c, p))

                if mins:

                    straddle = (sum(mins) * 10000) / ltp

                    vt = (
                        straddle * 100
                        / np.sqrt(minutes_to_expiry(target_exp, ts))
                    )

                else:
                    straddle = np.nan
                    vt = np.nan

                return pd.Series([straddle, vt])

            matrix[["straddle_3atm", "vt_3atm"]] = matrix.apply(
                calc_straddle_vt,
                axis=1
            )

            ordered = [
                "underlying_ltp",
                "straddle_3atm",
                "vt_3atm",
            ] + [
                c for c in matrix.columns
                if c not in (
                    "underlying_ltp",
                    "straddle_3atm",
                    "vt_3atm",
                )
            ]

            matrix = matrix[ordered]

            # --------------------------------------------------
            # SAVE
            # --------------------------------------------------
            out_dir = BASE_OUT_DIR / f"{UNDERLYING}_{dte}dte"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{UNDERLYING}_matrix_{fmt_yyyymmdd(day)}.csv"
            matrix.to_csv(out)
            print(f"saved {out.name}")


if __name__ == "__main__":
    main()
