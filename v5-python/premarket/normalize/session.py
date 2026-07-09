"""Trading session conversion: IST->UTC, US session windows."""
from datetime import datetime, time, timezone, timedelta
from typing import Optional

import pytz


# Timezone objects
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC
US_EASTERN = pytz.timezone("America/New_York")

# US trading session windows (EST/EDT in local time, converted to UTC range strings)
# Format: "HHMM-HHMM|HHMM-HHMM|..." (UTC times, pipe-separated for multiple sessions)

US_EQUITY_SESSION = "13:30-20:00|13:00-21:00"  # Regular + after-hours (EST/EDT -> UTC conversion)
US_PRE_MARKET = "07:00-13:30"  # Pre-market session
US_AFTER_HOURS = "16:00-20:00"  # After-hours session


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
    Convert IST trading session string to UTC.
    Input: "HHMM-HHMM" (IST), Output: "HH:MM-HH:MM" (UTC)
    """
    try:
        start, end = ist_session.split("-")
        utc_start = ist_hhmm_to_utc(f"{start[:2]}:{start[2:]}")
        utc_end = ist_hhmm_to_utc(f"{end[:2]}:{end[2:]}")
        if utc_start and utc_end:
            return f"{utc_start}-{utc_end}"
    except (ValueError, AttributeError):
        pass
    return ""


def trading_session_for_xnas() -> str:
    """Get US equity trading session (XNAS) in UTC."""
    return US_EQUITY_SESSION


def trading_session_for_xcbo_equity() -> str:
    """Get XCBO equity session (OPRA) in UTC."""
    return US_EQUITY_SESSION


def trading_session_for_xcbo_index() -> str:
    """Get XCBO index session in UTC."""
    return US_EQUITY_SESSION


def trading_session_for_xcme() -> str:
    """Get XCME (CME/GLBX) session in UTC (nearly 24-hour with gaps)."""
    return "16:00-15:59"  # Simplified; actual CME sessions are complex


def get_us_session_windows() -> dict:
    """Get all US session windows."""
    return {
        "xnas": trading_session_for_xnas(),
        "xcbo_equity": trading_session_for_xcbo_equity(),
        "xcbo_index": trading_session_for_xcbo_index(),
        "xcme": trading_session_for_xcme(),
    }
