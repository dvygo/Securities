#!/usr/bin/env python3
"""Filter ``raw/XCBO-DATABENTO.csv`` → ``normalized/XCBO-DATABENTO.csv`` (near-term OPRA weeklies).

Reads Live symbology dump columns (``stype_out_symbol``, etc.). Keeps rows whose OCC
expiration is:

  - **Listed equity** (not SPXW): this week's and next week's standard weekly expiry
    (XNYS Friday on/after as-of, or prior session if Friday is closed).
  - **SPXW**: expiration in ``[as_of, as_of + 14 calendar days]``.

Dedupes by ``stype_out_symbol`` (first row wins). Other v3 dumps are not touched.

Default paths (under this script's directory):
  ``YYYYMMDD/raw/XCBO-DATABENTO.csv`` → ``YYYYMMDD/normalized/XCBO-DATABENTO.csv``

Requires: exchange-calendars, pandas (repo ``requirements.txt``).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

from symbology_paths import XCBO_CSV, day_dir, day_normalized_csv, day_raw_csv, repo_root

_V4_DIR = repo_root()
_OPRA_OCC_TAIL = re.compile(r"(\d{6})([CP])(\d{8})\s*$", re.I)


def _default_csv_paths(as_of: date | None = None) -> tuple[Path, Path]:
    d = day_dir(as_of=as_of, root=_V4_DIR)
    return day_raw_csv(d, XCBO_CSV), day_normalized_csv(d, XCBO_CSV)


def _yymmdd_to_iso(yymmdd: str) -> date | None:
    try:
        yi = int(yymmdd[:2])
        year = 2000 + yi if yi < 70 else 1900 + yi
        return date(year, int(yymmdd[2:4]), int(yymmdd[4:6]))
    except (ValueError, TypeError):
        return None


def _parse_opra_occ(symbol: str) -> tuple[str, date | None]:
    """Return (underlying, expiration) from ``stype_out_symbol`` / OCC tail."""
    s = (symbol or "").strip()
    m = _OPRA_OCC_TAIL.search(s)
    if not m:
        return "", None
    yymmdd = m.group(1)
    prefix = s[: m.start()]
    und = re.sub(r"\s+", "", prefix).upper() or prefix.strip().upper()
    return und, _yymmdd_to_iso(yymmdd)


def _anchor_fridays(as_of: date) -> tuple[date, date]:
    wd = as_of.weekday()
    days_to_fri = (4 - wd) % 7
    f1 = as_of + timedelta(days=days_to_fri)
    return f1, f1 + timedelta(days=7)


def _weekly_expiry_dates(as_of: date) -> frozenset[date]:
    """This week and next week equity weekly expiry (XNYS-adjusted Fridays)."""
    cal = xcals.get_calendar("XNYS")

    def map_friday(friday: date) -> date:
        ts = pd.Timestamp(friday)
        if cal.is_session(ts):
            return friday
        return cal.previous_session(ts).date()

    f1, f2 = _anchor_fridays(as_of)
    return frozenset({map_friday(f1), map_friday(f2)})


def _keep_opra_row(underlying: str, exp: date | None, as_of: date, weekly: frozenset[date]) -> bool:
    if exp is None:
        return False
    u = underlying.strip().upper()
    if u == "SPXW":
        end = as_of + timedelta(days=14)
        return as_of <= exp <= end
    return exp in weekly


def keep_opra_pillar(
    rows: list[dict[str, str]],
    *,
    as_of: date,
    sym_col: str = "stype_out_symbol",
) -> list[dict[str, str]]:
    """Filter symbology rows to near-term OPRA expiries; dedupe by ``sym_col``."""
    weekly = _weekly_expiry_dates(as_of)
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        sym = (row.get(sym_col) or "").strip()
        und, exp = _parse_opra_occ(sym)
        if not _keep_opra_row(und, exp, as_of, weekly):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(row)
    return out


def _field_ci(fieldnames: list[str], logical: str) -> str | None:
    want = logical.lower()
    for f in fieldnames:
        if f.lower() == want:
            return f
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date-dir",
        default="",
        metavar="YYYYMMDD",
        help="Day folder under v4/ (default: today)",
    )
    p.add_argument("--input", type=Path, default=None, help="Unstripped symbology CSV")
    p.add_argument("--output", type=Path, default=None, help="Stripped output CSV")
    p.add_argument(
        "--as-of",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Reference date for expiry filter (default: --date-dir or today)",
    )
    args = p.parse_args(argv)

    as_of = date.today()
    if (args.date_dir or "").strip():
        try:
            as_of = datetime.strptime(args.date_dir.strip(), "%Y%m%d").date()
        except ValueError:
            print("--date-dir must be YYYYMMDD", file=sys.stderr)
            return 1
    elif args.as_of:
        try:
            as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            print("--as-of must be YYYY-MM-DD", file=sys.stderr)
            return 1

    default_in, default_out = _default_csv_paths(as_of=as_of)
    in_path: Path = args.input or default_in
    if not in_path.is_absolute():
        in_path = (_V4_DIR / in_path).resolve()
    if not in_path.is_file():
        print(f"Input not found: {in_path}", file=sys.stderr)
        return 1

    out_path: Path = args.output or default_out
    if not out_path.is_absolute():
        out_path = (_V4_DIR / out_path).resolve()

    weekly = _weekly_expiry_dates(as_of)
    n_in = 0
    with in_path.open(newline="", encoding="utf-8-sig", errors="replace") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            print("CSV has no header row.", file=sys.stderr)
            return 1
        names = list(reader.fieldnames)
        sym_key = _field_ci(names, "stype_out_symbol")
        if not sym_key:
            print("CSV must include 'stype_out_symbol'.", file=sys.stderr)
            return 1
        all_rows = list(reader)
        n_in = len(all_rows)

    kept = keep_opra_pillar(all_rows, as_of=as_of, sym_col=sym_key)
    n_out = len(kept)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as fout:
        w = csv.DictWriter(fout, fieldnames=names, extrasaction="ignore")
        w.writeheader()
        for row in kept:
            w.writerow(row)

    print(
        f"As-of {as_of}: weekly expiries (non-SPXW) = {sorted(weekly)}; "
        f"SPXW window [{as_of} .. {as_of + timedelta(days=14)}]",
        file=sys.stderr,
    )
    print(
        f"Read {n_in} rows, wrote {n_out} rows (deduped by {sym_key!r}) -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
