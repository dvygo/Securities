"""Exchange session helpers for v3 symbology normalization.

Uses ``exchange_calendars`` (e.g. CME Globex, NYSE XNYS) for session checks.
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def _session_ts(d: date) -> pd.Timestamp:
    # exchange_calendars requires timezone-naive timestamps for is_session.
    return pd.Timestamp(d)


def _calendar(name: str):
    import exchange_calendars as xcals

    return xcals.get_calendar(name)


def is_session(cal_name: str, d: date) -> bool:
    cal = _calendar(cal_name)
    return bool(cal.is_session(_session_ts(d)))
