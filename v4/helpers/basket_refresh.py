#!/usr/bin/env python3
"""Resolve basket templates into today's contract CSVs.

Reads ``constituents/baskets/``, joins ``YYYYMMDD/normalized/X*-FYERS.csv``,
writes ``constituents/contracts/YYYYMMDD/{basket}.csv`` with ``exToken`` / ``exSymbol``.

  python basket_refresh.py --date-dir 20260529
  python basket_refresh.py --basket NIFTY_FNO_FUTURES_NEAR --dry-run
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from symbology_paths import (
    RAW_SUBDIR,
    XBFO_CSV,
    XMCX_CSV,
    XNFO_CSV,
    XNSE_CSV,
    day_dir,
    repo_root,
)

_BASKETS_DIR = repo_root() / "constituents" / "baskets"
_CONTRACTS_DIR = repo_root() / "constituents" / "contracts"

_SPOTS_BASKET = "NIFTY_FNO_EQUITY_SPOTS.csv"
_NSE_INDEX_BASKET = "NSE_INDEX_FUTURES.csv"
_BSE_INDEX_BASKET = "BSE_INDEX_FUTURES.csv"
_MCX_BASKET = "MCX_FUTURES.csv"

_OUTPUT_COLUMNS: tuple[str, ...] = (
    "date",
    "exchange",
    "underlying",
    "instrument",
    "expiration",
    "strike",
    "multiplier",
    "exToken",
    "exSymbol",
    "displaySymbol",
)

_ALL_BASKET_NAMES: tuple[str, ...] = (
    "NIFTY_FNO_EQUITY_SPOTS",
    "NIFTY_FNO_FUTURES_NEAR",
    "NIFTY_FNO_FUTURES_ALL",
    "NSE_INDEX_FUTURES",
    "BSE_INDEX_FUTURES",
    "MCX_FUTURES",
    "ALL_INDEX_FUTURES",
)

_FUT_TAIL = re.compile(
    r"^[^:]+:(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))FUT$",
    re.IGNORECASE,
)
_EQ_TAIL = re.compile(r"^[^:]+:(.+)-EQ$", re.IGNORECASE)


@dataclass
class RefreshStats:
    written: int = 0
    dropped_missing: int = 0
    dropped_expired: int = 0
    skipped_no_fut: int = 0

    def merge(self, other: RefreshStats) -> None:
        self.written += other.written
        self.dropped_missing += other.dropped_missing
        self.dropped_expired += other.dropped_expired
        self.skipped_no_fut += other.skipped_no_fut


@dataclass
class SymbologyIndex:
    by_symbol: dict[str, dict[str, str]] = field(default_factory=dict)
    futures_by_underlying: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    display_by_symbol: dict[str, str] = field(default_factory=dict)


def load_basket_symbols(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def parse_eq_underlying(ticker: str) -> str:
    m = _EQ_TAIL.match(ticker.strip())
    return m.group(1).upper() if m else ""


def parse_fut_root(ticker: str) -> str:
    m = _FUT_TAIL.match(ticker.strip())
    return m.group(1).upper() if m else ""


def _int_field(row: dict[str, str], key: str) -> int:
    raw = (row.get(key) or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _is_fut_row(row: dict[str, str]) -> bool:
    sym = (row.get("symbol") or "").strip().upper()
    return sym.endswith("FUT")


def _load_fyers_display_names(raw_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw_path.is_file():
        return out
    with raw_path.open(newline="", encoding="utf-8-sig", errors="replace") as fin:
        reader = csv.DictReader(fin)
        for row in reader:
            ticker = (row.get("symbolTicker") or "").strip()
            label = (row.get("symbol") or "").strip()
            if ticker and label:
                out[ticker] = label
    return out


def load_symbology_csv(path: Path, *, raw_path: Path | None = None) -> SymbologyIndex:
    idx = SymbologyIndex()
    if raw_path is not None:
        idx.display_by_symbol = _load_fyers_display_names(raw_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames or "symbol" not in reader.fieldnames:
            raise ValueError(f"{path}: missing symbol column")
        for row in reader:
            sym = (row.get("symbol") or "").strip()
            if sym:
                idx.by_symbol[sym] = row
            if _is_fut_row(row):
                und = (row.get("underlying") or "").strip().upper()
                if und:
                    idx.futures_by_underlying.setdefault(und, []).append(row)
    return idx


def _infer_instrument(row: dict[str, str]) -> str:
    sym = (row.get("symbol") or "").strip().upper()
    if sym.endswith("-EQ"):
        return "SPOT"
    if sym.endswith("FUT"):
        return "FUT"
    exp = _int_field(row, "expiration")
    if exp == 0:
        return "SPOT"
    return "FUT"


def to_contract_row(
    row: dict[str, str],
    *,
    as_of: date,
    display_by_symbol: dict[str, str] | None = None,
) -> dict[str, str]:
    strike_raw = (row.get("strike") or "").strip()
    exp = _int_field(row, "expiration")
    sym = (row.get("symbol") or "").strip()
    display = (display_by_symbol or {}).get(sym, "")
    return {
        "date": as_of.strftime("%Y%m%d"),
        "exchange": (row.get("exchange") or "").strip(),
        "underlying": (row.get("underlying") or "").strip(),
        "instrument": _infer_instrument(row),
        "expiration": str(exp) if exp else "0",
        "strike": strike_raw if strike_raw else ("0" if exp == 0 else ""),
        "multiplier": str(_int_field(row, "multiplier") or ""),
        "exToken": (row.get("token") or "").strip(),
        "exSymbol": sym,
        "displaySymbol": display,
    }


def _live_futures(
    idx: SymbologyIndex,
    underlying: str,
    *,
    as_of_int: int,
) -> list[dict[str, str]]:
    rows = idx.futures_by_underlying.get(underlying.upper(), [])
    live: list[dict[str, str]] = []
    for row in rows:
        exp = _int_field(row, "expiration")
        if exp and exp >= as_of_int:
            live.append(row)
    return live


def _pick_near(rows: list[dict[str, str]]) -> dict[str, str] | None:
    if not rows:
        return None
    return min(rows, key=lambda r: _int_field(r, "expiration"))


def resolve_spots(
    spots_basket: Path,
    sym: SymbologyIndex,
    *,
    as_of: date,
) -> tuple[list[dict[str, str]], RefreshStats]:
    stats = RefreshStats()
    out: list[dict[str, str]] = []
    for ticker in load_basket_symbols(spots_basket):
        row = sym.by_symbol.get(ticker)
        if row is None:
            stats.dropped_missing += 1
            continue
        out.append(to_contract_row(row, as_of=as_of, display_by_symbol=sym.display_by_symbol))
    stats.written = len(out)
    return out, stats


def resolve_stock_futs(
    spots_basket: Path,
    sym: SymbologyIndex,
    *,
    as_of: date,
    near: bool,
) -> tuple[list[dict[str, str]], RefreshStats]:
    stats = RefreshStats()
    as_of_int = int(as_of.strftime("%Y%m%d"))
    out: list[dict[str, str]] = []
    underlyings: list[str] = []
    for ticker in load_basket_symbols(spots_basket):
        und = parse_eq_underlying(ticker)
        if und:
            underlyings.append(und)

    for und in underlyings:
        live = _live_futures(sym, und, as_of_int=as_of_int)
        if not live:
            stats.skipped_no_fut += 1
            continue
        if near:
            picked = _pick_near(live)
            if picked:
                out.append(
                    to_contract_row(picked, as_of=as_of, display_by_symbol=sym.display_by_symbol),
                )
        else:
            for row in sorted(live, key=lambda r: _int_field(r, "expiration")):
                out.append(
                    to_contract_row(row, as_of=as_of, display_by_symbol=sym.display_by_symbol),
                )

    stats.written = len(out)
    return out, stats


def resolve_index_futs_near(
    template_basket: Path,
    sym: SymbologyIndex,
    *,
    as_of: date,
) -> tuple[list[dict[str, str]], RefreshStats]:
    stats = RefreshStats()
    as_of_int = int(as_of.strftime("%Y%m%d"))
    roots: list[str] = []
    seen: set[str] = set()
    for ticker in load_basket_symbols(template_basket):
        root = parse_fut_root(ticker)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)

    out: list[dict[str, str]] = []
    for root in roots:
        live = _live_futures(sym, root, as_of_int=as_of_int)
        if not live:
            stats.skipped_no_fut += 1
            continue
        picked = _pick_near(live)
        if picked:
            out.append(to_contract_row(picked, as_of=as_of, display_by_symbol=sym.display_by_symbol))

    stats.written = len(out)
    return out, stats


def resolve_mcx_futs_all(
    template_basket: Path,
    sym: SymbologyIndex,
    *,
    as_of: date,
) -> tuple[list[dict[str, str]], RefreshStats]:
    stats = RefreshStats()
    as_of_int = int(as_of.strftime("%Y%m%d"))
    roots: list[str] = []
    seen: set[str] = set()
    for ticker in load_basket_symbols(template_basket):
        root = parse_fut_root(ticker)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)

    out: list[dict[str, str]] = []
    for root in roots:
        live = _live_futures(sym, root, as_of_int=as_of_int)
        if not live:
            stats.skipped_no_fut += 1
            continue
        for row in sorted(live, key=lambda r: _int_field(r, "expiration")):
            out.append(to_contract_row(row, as_of=as_of, display_by_symbol=sym.display_by_symbol))

    stats.written = len(out)
    return out, stats


def write_contract_csv(path: Path, rows: list[dict[str, str]], *, dry_run: bool) -> None:
    if dry_run:
        print(f"dry-run: would write {len(rows)} rows -> {path}", file=sys.stderr)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fout:
        writer = csv.DictWriter(fout, fieldnames=_OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows -> {path}", file=sys.stderr)


def contracts_dir(*, as_of: date) -> Path:
    return _CONTRACTS_DIR / as_of.strftime("%Y%m%d")


def _norm_raw_paths(day: Path, csv_name: str) -> tuple[Path, Path]:
    return day / "normalized" / csv_name, day / RAW_SUBDIR / csv_name


def refresh_basket(
    name: str,
    *,
    as_of: date,
    day: Path,
    dry_run: bool,
) -> tuple[list[dict[str, str]], RefreshStats]:
    spots_path = _BASKETS_DIR / _SPOTS_BASKET
    out_path = contracts_dir(as_of=as_of) / f"{name}.csv"
    stats = RefreshStats()

    if name == "NIFTY_FNO_EQUITY_SPOTS":
        norm_path, raw_path = _norm_raw_paths(day, XNSE_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_spots(spots_path, sym, as_of=as_of)
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "NIFTY_FNO_FUTURES_NEAR":
        norm_path, raw_path = _norm_raw_paths(day, XNFO_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_stock_futs(spots_path, sym, as_of=as_of, near=True)
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "NIFTY_FNO_FUTURES_ALL":
        norm_path, raw_path = _norm_raw_paths(day, XNFO_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_stock_futs(spots_path, sym, as_of=as_of, near=False)
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "NSE_INDEX_FUTURES":
        norm_path, raw_path = _norm_raw_paths(day, XNFO_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_index_futs_near(
            _BASKETS_DIR / _NSE_INDEX_BASKET, sym, as_of=as_of,
        )
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "BSE_INDEX_FUTURES":
        norm_path, raw_path = _norm_raw_paths(day, XBFO_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_index_futs_near(
            _BASKETS_DIR / _BSE_INDEX_BASKET, sym, as_of=as_of,
        )
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "MCX_FUTURES":
        norm_path, raw_path = _norm_raw_paths(day, XMCX_CSV)
        sym = load_symbology_csv(norm_path, raw_path=raw_path)
        rows, stats = resolve_mcx_futs_all(
            _BASKETS_DIR / _MCX_BASKET, sym, as_of=as_of,
        )
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    if name == "ALL_INDEX_FUTURES":
        nfo_norm, nfo_raw = _norm_raw_paths(day, XNFO_CSV)
        xbfo_norm, xbfo_raw = _norm_raw_paths(day, XBFO_CSV)
        mcx_norm, mcx_raw = _norm_raw_paths(day, XMCX_CSV)
        nfo = load_symbology_csv(nfo_norm, raw_path=nfo_raw)
        xbfo = load_symbology_csv(xbfo_norm, raw_path=xbfo_raw)
        mcx = load_symbology_csv(mcx_norm, raw_path=mcx_raw)
        nse_rows, nse_stats = resolve_index_futs_near(
            _BASKETS_DIR / _NSE_INDEX_BASKET, nfo, as_of=as_of,
        )
        bse_rows, bse_stats = resolve_index_futs_near(
            _BASKETS_DIR / _BSE_INDEX_BASKET, xbfo, as_of=as_of,
        )
        mcx_rows, mcx_stats = resolve_mcx_futs_all(
            _BASKETS_DIR / _MCX_BASKET, mcx, as_of=as_of,
        )
        rows = nse_rows + bse_rows + mcx_rows
        stats.merge(nse_stats)
        stats.merge(bse_stats)
        stats.merge(mcx_stats)
        stats.written = len(rows)
        write_contract_csv(out_path, rows, dry_run=dry_run)
        return rows, stats

    raise KeyError(f"unknown basket {name!r}")


def refresh_all(*, as_of: date, day: Path, dry_run: bool) -> RefreshStats:
    total = RefreshStats()
    for name in _ALL_BASKET_NAMES:
        if name == "ALL_INDEX_FUTURES":
            continue
        _, stats = refresh_basket(name, as_of=as_of, day=day, dry_run=dry_run)
        total.merge(stats)
        print(
            f"{name}: written={stats.written} missing={stats.dropped_missing} "
            f"no_fut={stats.skipped_no_fut}",
            file=sys.stderr,
        )
    _, union_stats = refresh_basket("ALL_INDEX_FUTURES", as_of=as_of, day=day, dry_run=dry_run)
    print(
        f"ALL_INDEX_FUTURES: written={union_stats.written} no_fut={union_stats.skipped_no_fut}",
        file=sys.stderr,
    )
    total.merge(union_stats)
    return total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date-dir", default="", metavar="YYYYMMDD", help="symbology day folder")
    p.add_argument(
        "--basket",
        default="all",
        choices=[*_ALL_BASKET_NAMES, "all"],
        help="which basket to refresh (default: all)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.date_dir.strip():
        try:
            as_of = datetime.strptime(args.date_dir.strip(), "%Y%m%d").date()
        except ValueError:
            print("--date-dir must be YYYYMMDD", file=sys.stderr)
            return 1
    else:
        as_of = date.today()

    day = day_dir(as_of=as_of, root=repo_root())
    if not (day / "normalized" / XNSE_CSV).is_file():
        print(f"error: missing normalized symbology under {day}", file=sys.stderr)
        return 1

    print(f"basket_refresh: as_of={as_of} day={day}", file=sys.stderr)

    if args.basket == "all":
        refresh_all(as_of=as_of, day=day, dry_run=args.dry_run)
    else:
        _, stats = refresh_basket(args.basket, as_of=as_of, day=day, dry_run=args.dry_run)
        print(
            f"{args.basket}: written={stats.written} missing={stats.dropped_missing} "
            f"no_fut={stats.skipped_no_fut}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
