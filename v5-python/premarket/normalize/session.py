"""Trading session conversion: IST->UTC, US 3-part (pre/main/after) session windows."""
from datetime import date, datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pytz


# Timezone objects (Fyers/IST path)
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC

US_EASTERN = ZoneInfo("America/New_York")

# US session profiles: (pre, main, after) as (start_h, start_m, end_h, end_m) in
# Eastern local time. Mirrors v4-golang's internal/normalize/us_session.go so
# both normalizers emit the same 3-segment "HHMM-HHMM|HHMM-HHMM|HHMM-HHMM" UTC
# string, correctly shifted across DST.
_XNAS_SESSION = ((4, 0, 9, 30), (9, 30, 16, 0), (16, 0, 20, 0))
_XCBO_EQUITY_SESSION = ((7, 30, 9, 25), (9, 30, 16, 0), (16, 0, 16, 15))
_XCBO_INDEX_SESSION = ((20, 15, 9, 25), (9, 30, 16, 15), (16, 15, 17, 0))
_XCME_GLOBEX_SESSION = ((18, 0, 9, 30), (9, 30, 16, 15), (16, 15, 17, 0))


def _et_window_to_utc_slot(ref: date, start_h: int, start_m: int, end_h: int, end_m: int) -> str:
    """Convert one Eastern-local HH:MM-HH:MM window (on `ref` date) to a UTC HHMM-HHMM slot, wrapping past midnight if needed."""
    start_et = datetime(ref.year, ref.month, ref.day, start_h, start_m, tzinfo=US_EASTERN)
    end_et = datetime(ref.year, ref.month, ref.day, end_h, end_m, tzinfo=US_EASTERN)
    if end_et <= start_et:
        from datetime import timedelta
        end_et = end_et + timedelta(days=1)
    start_utc = start_et.astimezone(UTC)
    end_utc = end_et.astimezone(UTC)
    return f"{start_utc.strftime('%H%M')}-{end_utc.strftime('%H%M')}"


def _session_utc(ref: date, profile: tuple) -> str:
    slots = [_et_window_to_utc_slot(ref, *window) for window in profile]
    return "|".join(slots)


def ist_hhmm_to_utc(ist_time_str: str) -> str:
    """
    Convert IST HH:MM to UTC HH:MM.
    IST is UTC+5:30, so subtract 5:30 to get UTC.
    """
    try:
        parts = ist_time_str.split(":")
        if len(parts) != 2:
            return ""

        hour = int(parts[0])
        minute = int(parts[1])

        # Create IST time (arbitrary date)
        ist_dt = IST.localize(datetime(2024, 1, 1, hour, minute, 0))
        utc_dt = ist_dt.astimezone(UTC)

        return utc_dt.strftime("%H:%M")
    except (ValueError, IndexError):
        return ""


def trading_session_ist_to_utc(ist_session: str) -> str:
    """
    Convert an IST trading session string to UTC, matching v4-golang's
    TradingSessionISTToUTC. The real Fyers tradingSession field is
    pipe-separated for multi-segment sessions and has a trailing colon,
    e.g. "0915-1530|1815-1915:" -> "0330-1000|1245-1345".
    """
    s = ist_session.strip().rstrip(":")
    if not s:
        return ""
    slots = []
    for part in s.split("|"):
        part = part.strip()
        if not part or "-" not in part:
            return ""
        start, end = part.split("-", 1)
        utc_start = ist_hhmm_to_utc(f"{start[:2]}:{start[2:]}")
        utc_end = ist_hhmm_to_utc(f"{end[:2]}:{end[2:]}")
        if not utc_start or not utc_end:
            return ""
        slots.append(f"{utc_start.replace(':', '')}-{utc_end.replace(':', '')}")
    return "|".join(slots)


# NSE/BSE pre-open auction order-entry window. Not carried on Fyers'
# tradingSession field at all (it only has continuous + evening sessions),
# so it must be injected rather than trusted from the raw feed -- same
# principle as the independently-computed US 3-part sessions. India has no
# DST, so a fixed +5:30 offset is exact (no need for the zoneinfo path).
NSE_PREOPEN_UTC = "0330-0338"


def trading_session_for_xnas(ref: Optional[date] = None) -> str:
    """XNAS 3-part session (pre-market/regular/after-hours) in UTC, DST-aware."""
    return _session_utc(ref or date.today(), _XNAS_SESSION)


def trading_session_for_xcbo_equity(ref: Optional[date] = None) -> str:
    """XCBO single-name equity options 3-part session in UTC, DST-aware."""
    return _session_utc(ref or date.today(), _XCBO_EQUITY_SESSION)


def trading_session_for_xcbo_index(ref: Optional[date] = None) -> str:
    """XCBO index options (SPX/SPXW/VIX/RUT) 3-part session in UTC, DST-aware."""
    return _session_utc(ref or date.today(), _XCBO_INDEX_SESSION)


def trading_session_for_xcme(ref: Optional[date] = None) -> str:
    """XCME/GLBX 3-part Globex session in UTC, DST-aware (excludes daily maintenance halt)."""
    return _session_utc(ref or date.today(), _XCME_GLOBEX_SESSION)


def get_us_session_windows(ref: Optional[date] = None) -> dict:
    """Get all US session windows for a given reference date."""
    return {
        "xnas": trading_session_for_xnas(ref),
        "xcbo_equity": trading_session_for_xcbo_equity(ref),
        "xcbo_index": trading_session_for_xcbo_index(ref),
        "xcme": trading_session_for_xcme(ref),
    }
