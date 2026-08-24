"""Basket/constituents resolution: build daily contract lists.

Most basket definition files are frozen snapshots of a specific expiry month (e.g. "26MAYFUT")
from whenever they were generated, so an exact-script match against them
goes stale the moment that month's contracts expire. Futures baskets are
instead resolved by extracting the underlying_root from each entry and
rolling to the nearest (or all) still-live contract for that root in
today's normalized data.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from . import parquet_export, paths, runner


# "NSE:360ONE-EQ" -> "360ONE"
EQ_TAIL_REGEX = re.compile(r"^[^:]+:(.+)-EQ$")
# "NSE:360ONE26JULFUT" -> "360ONE" (root is non-greedy so it stops at the
# first valid MONYY it can match).
FUT_TAIL_REGEX = re.compile(
    r"^[^:]+:(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))FUT$"
)
# "MCX:CRUDEOIL26AUG10000CE" -> "CRUDEOIL" (root non-greedy up to the first MONYY,
# then strike digits, then CE/PE). Strike may be fractional (e.g. "...100.25PE").
OPT_TAIL_REGEX = re.compile(
    r"^[^:]+:(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))"
    r"\d+(?:\.\d+)?(CE|PE)$"
)


def _load_basket_scripts(basket_file: Path) -> List[str]:
    """Read a basket file: one already-resolved contract script per line ("NSE:360ONE-EQ",
    "MCX:GOLD26AUGFUT", ...). Blank lines and "#"-prefixed generator-provenance comments
    are skipped -- these files are plain lists, not tabular CSV with a header."""
    scripts = []
    with open(basket_file, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            scripts.append(line)
    return scripts


def _parse_eq_root(script: str) -> str:
    m = EQ_TAIL_REGEX.match(script.strip())
    return m.group(1).upper() if m else ""


def _parse_fut_root(script: str) -> str:
    m = FUT_TAIL_REGEX.match(script.strip())
    return m.group(1).upper() if m else ""


def _parse_opt_root(script: str) -> str:
    m = OPT_TAIL_REGEX.match(script.strip())
    return m.group(1).upper() if m else ""


def _is_future_row(row: dict) -> bool:
    t = (row.get("scriptInstrumentType") or "").strip().upper()
    if t.startswith("FUT"):
        return True
    return (row.get("script") or "").strip().upper().endswith("FUT")


def _is_option_row(row: dict) -> bool:
    if (row.get("optionType") or "").strip():
        return True
    t = (row.get("scriptInstrumentType") or "").strip().upper()
    if t.startswith("OPT"):
        return True
    s = (row.get("script") or "").strip().upper()
    return s.endswith("CE") or s.endswith("PE")


def _int_field(row: dict, key: str) -> int:
    raw = (row.get(key) or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


class SymIndex:
    """Per-MIC lookup built from that segment's normalized CSV: exact script ->
    row, and live-future candidates grouped by underlying_root for rolling."""

    def __init__(self, exchange_mic: str, normalized_csv: Path):
        self.exchange_mic = exchange_mic
        self.by_script: Dict[str, dict] = {}
        self.futures_by_root: Dict[str, List[dict]] = {}
        self.options_by_root: Dict[str, List[dict]] = {}

        if not normalized_csv.exists():
            return
        for row in parquet_export.read_rows(normalized_csv):
            script = (row.get("script") or "").strip()
            if script:
                self.by_script[script] = row
            root = (row.get("underlying_root") or "").strip().upper()
            if root and _is_future_row(row):
                self.futures_by_root.setdefault(root, []).append(row)
            elif root and _is_option_row(row):
                self.options_by_root.setdefault(root, []).append(row)

    def live_futures(self, root: str, as_of_ns: int) -> List[dict]:
        return [r for r in self.futures_by_root.get(root.upper(), []) if _int_field(r, "expiration") >= as_of_ns]

    def live_options(self, root: str, as_of_ns: int) -> List[dict]:
        return [r for r in self.options_by_root.get(root.upper(), []) if _int_field(r, "expiration") >= as_of_ns]

    def to_contract_row(self, row: dict, as_of: str) -> dict:
        out = {"date": as_of, "exchange": self.exchange_mic}
        for col in paths.NORMALIZED_COLUMNS:
            out[col] = row.get(col, "")
        return out


def _pick_nearest_expiry(rows: List[dict]) -> Optional[dict]:
    if not rows:
        return None
    return min(rows, key=lambda r: _int_field(r, "expiration"))


def _as_of_start_ns(as_of: str) -> int:
    """Nanosecond epoch UTC for the start of as_of's calendar day. Must stay
    UTC to compare against "expiration" (databento_norm.py's
    glbx_expiration_ns/OCC path, fields.py's _expiration_ns), which are
    themselves UTC."""
    d = datetime.strptime(as_of, "%Y%m%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp()) * 10**9


def _sym_index(as_of: str, mic: str, cache: Dict[str, SymIndex]) -> SymIndex:
    if mic not in cache:
        output_csv, _, _ = paths.FYERS_MIC_BUNDLES[mic]
        cache[mic] = SymIndex(mic, paths.normalized_dir(as_of) / output_csv)
    return cache[mic]


def _resolve_by_script(name: str, template: Path, idx: SymIndex, as_of: str) -> List[dict]:
    scripts = _load_basket_scripts(template)
    rows, missing = [], 0
    for script in scripts:
        row = idx.by_script.get(script)
        if row is None:
            missing += 1
            continue
        rows.append(idx.to_contract_row(row, as_of))
    if missing:
        print(f"    {name}: {missing}/{len(scripts)} constituents not found in today's contracts")
    return rows


def _resolve_equity_futures(name: str, spots_template: Path, idx: SymIndex, as_of: str, near_only: bool) -> List[dict]:
    scripts = _load_basket_scripts(spots_template)
    as_of_ns = _as_of_start_ns(as_of)
    rows, no_fut, seen = [], 0, set()
    for script in scripts:
        root = _parse_eq_root(script)
        if not root or root in seen:
            continue
        seen.add(root)

        live = idx.live_futures(root, as_of_ns)
        if not live:
            no_fut += 1
            continue
        if near_only:
            picked = _pick_nearest_expiry(live)
            if picked:
                rows.append(idx.to_contract_row(picked, as_of))
        else:
            for r in sorted(live, key=lambda r: _int_field(r, "expiration")):
                rows.append(idx.to_contract_row(r, as_of))
    if no_fut:
        print(f"    {name}: {no_fut}/{len(seen)} underlyings have no live future today")
    return rows


def _resolve_index_futures(name: str, template: Path, idx: SymIndex, as_of: str, near_only: bool) -> List[dict]:
    scripts = _load_basket_scripts(template)
    as_of_ns = _as_of_start_ns(as_of)
    rows, no_fut, seen = [], 0, set()
    for script in scripts:
        root = _parse_fut_root(script)
        if not root or root in seen:
            continue
        seen.add(root)

        live = idx.live_futures(root, as_of_ns)
        if not live:
            no_fut += 1
            continue
        if near_only:
            picked = _pick_nearest_expiry(live)
            if picked:
                rows.append(idx.to_contract_row(picked, as_of))
        else:
            for r in sorted(live, key=lambda r: _int_field(r, "expiration")):
                rows.append(idx.to_contract_row(r, as_of))
    if no_fut:
        print(f"    {name}: {no_fut}/{len(scripts)} roots rolled to no live future")
    return rows


def _resolve_option_chain(name: str, template: Path, idx: SymIndex, as_of: str, num_expiries: int) -> List[dict]:
    """Roll an option-chain basket to its N nearest still-live expiries. The template
    only supplies the underlying root(s) (parsed from any option script in it); the
    concrete strikes/expiries in the file go stale and are ignored -- we re-pick the
    num_expiries nearest distinct expiries from today's data and emit their full chains
    (every CE/PE at every strike). Mirrors the futures roll, but keeps N expiries deep
    and all strikes instead of collapsing to one contract per root."""
    scripts = _load_basket_scripts(template)
    as_of_ns = _as_of_start_ns(as_of)
    rows, no_opt, seen = [], 0, set()
    for script in scripts:
        root = _parse_opt_root(script)
        if not root or root in seen:
            continue
        seen.add(root)

        live = idx.live_options(root, as_of_ns)
        if not live:
            no_opt += 1
            continue
        keep = set(sorted({_int_field(r, "expiration") for r in live})[:num_expiries])
        for r in sorted(live, key=lambda r: (_int_field(r, "expiration"), r.get("script", ""))):
            if _int_field(r, "expiration") in keep:
                rows.append(idx.to_contract_row(r, as_of))
    if no_opt:
        print(f"    {name}: {no_opt}/{len(seen)} roots have no live option today")
    return rows


def _refresh(name: str, as_of: str, cache: Dict[str, SymIndex]) -> List[dict]:
    """Resolve one basket's constituent rows.
    Basket names are standardized to {MIC}_{purpose} and match their definition CSV's
    filename 1:1, except where noted (futures-roll baskets derive roots from a spots/
    equity basket rather than their own frozen file, and ALL_INDEX_FUTURES has no file
    of its own -- it's the union of the three index-futures baskets)."""
    baskets_dir = paths.baskets_dir()

    if name == "XNSE_NIFTYFNO_EQUITY":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_by_script(name, baskets_dir / f"{name}.csv", idx, as_of)

    if name == "XNSE_NIFTYFNO_FUTURES_NEAR":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_equity_futures(name, baskets_dir / "XNSE_NIFTYFNO_EQUITY.csv", idx, as_of, near_only=True)

    if name == "XNSE_NIFTYFNO_FUTURES_ALL":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_equity_futures(name, baskets_dir / "XNSE_NIFTYFNO_EQUITY.csv", idx, as_of, near_only=False)

    if name == "XNSE_INDEX_FUTURES_NEAR":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_index_futures(name, baskets_dir / f"{name}.csv", idx, as_of, near_only=True)

    if name == "XNSE_INDEX_FUTURES_ALL":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_index_futures(name, baskets_dir / f"{name}.csv", idx, as_of, near_only=False)

    if name == "XBOM_INDEX_FUTURES":
        idx = _sym_index(as_of, "XBOM", cache)
        return _resolve_index_futures(name, baskets_dir / f"{name}.csv", idx, as_of, near_only=True)

    if name == "XIMC_FUTURES_ALL":
        idx = _sym_index(as_of, "XIMC", cache)
        return _resolve_index_futures(name, baskets_dir / f"{name}.csv", idx, as_of, near_only=False)

    if name in ("XIMC_CRUDE_NEAREST_NXTNEAREST", "XIMC_BULLDEX_NEAREST_NXTNEAREST"):
        idx = _sym_index(as_of, "XIMC", cache)
        return _resolve_option_chain(name, baskets_dir / f"{name}.csv", idx, as_of, num_expiries=2)

    if name == "ALL_INDEX_FUTURES":
        nse_rows = _resolve_index_futures(
            "XNSE_INDEX_FUTURES_NEAR", baskets_dir / "XNSE_INDEX_FUTURES_NEAR.csv", _sym_index(as_of, "XNSE", cache), as_of, near_only=True
        )
        xbom_rows = _resolve_index_futures(
            "XBOM_INDEX_FUTURES", baskets_dir / "XBOM_INDEX_FUTURES.csv", _sym_index(as_of, "XBOM", cache), as_of, near_only=True
        )
        ximc_rows = _resolve_index_futures(
            "XIMC_FUTURES_ALL", baskets_dir / "XIMC_FUTURES_ALL.csv", _sym_index(as_of, "XIMC", cache), as_of, near_only=False
        )
        return nse_rows + xbom_rows + ximc_rows

    if name in ("XNSE_NIFTY50_EQUITY", "XNSE_NIFTY100_EQUITY", "XNSE_NIFTY200_EQUITY", "XNSE_NIFTY500_EQUITY"):
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_by_script(name, baskets_dir / f"{name}.csv", idx, as_of)

    if name == "XNSE_NIFTY500_FUTURES":
        idx = _sym_index(as_of, "XNSE", cache)
        return _resolve_equity_futures(name, baskets_dir / "XNSE_NIFTY500_EQUITY.csv", idx, as_of, near_only=False)

    raise ValueError(f"unknown basket {name!r}")


def refresh_basket(name: str, as_of: str, dry_run: bool = False) -> Optional[pd.DataFrame]:
    """Refresh a single basket. Returns None if nothing resolved."""
    basket_file = paths.baskets_dir() / f"{name}.csv"
    if not basket_file.exists():
        print(f"    Basket {name} not found at {basket_file}")
        return None

    rows = _refresh(name, as_of, cache={})
    return pd.DataFrame(rows) if rows else None


def refresh_all(as_of: str, normalized_dir: Path, dry_run: bool = False) -> None:
    """Refresh all baskets for a given day."""
    contracts_day_dir = paths.contracts_day_dir(as_of)
    contracts_day_dir.mkdir(parents=True, exist_ok=True)

    cache: Dict[str, SymIndex] = {}

    for basket_name in paths.BASKET_NAMES:
        if dry_run:
            print(f"    Would refresh basket {basket_name}")
            continue

        try:
            rows = _refresh(basket_name, as_of, cache)
            if rows:
                output_path = contracts_day_dir / f"{basket_name}{parquet_export.SUFFIX}"
                parquet_export.write_rows(output_path, list(rows[0].keys()), rows)
                print(f"    Wrote {basket_name}: {len(rows)} contracts")
        except Exception as e:
            print(f"    Error refreshing {basket_name}: {e}")


def run(opts: runner.Opts) -> None:
    """Refresh basket constituents."""
    if opts.dry_run:
        print("DRY RUN: Would refresh baskets")
        return

    print("  Refreshing baskets...")
    normalized_dir = paths.normalized_dir(opts.date_dir)

    if not normalized_dir.exists():
        print("    No normalized data directory")
        return

    if opts.basket:
        # Single basket refresh
        try:
            df = refresh_basket(opts.basket, opts.date_dir, opts.dry_run)
            if df is not None:
                contracts_dir = paths.contracts_day_dir(opts.date_dir)
                contracts_dir.mkdir(parents=True, exist_ok=True)
                output_path = contracts_dir / f"{opts.basket}{parquet_export.SUFFIX}"
                parquet_export.write_rows(
                    output_path, list(df.columns), df.astype(str).to_dict("records")
                )
                print(f"    Wrote {opts.basket}: {len(df)} rows")
        except Exception as e:
            print(f"    Error refreshing {opts.basket}: {e}")
    else:
        # All baskets
        refresh_all(opts.date_dir, normalized_dir, opts.dry_run)
