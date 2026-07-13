"""Basket/constituents resolution: build daily contract lists."""
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd

from . import export, paths, runner


# Regex patterns for parsing ticker symbols
EQUITY_REGEX = re.compile(r"^([A-Z0-9\-&]+)$")  # Simple equity symbol
FUTURE_REGEX = re.compile(r"^([A-Z0-9\-&]+)-([A-Z]{3}\d{2})FUT$")  # ROOT-MONYYFUT
OPTION_REGEX = re.compile(r"^([A-Z0-9\-&]+)-([A-Z]{3}\d{2})([CP])(\d+)$")  # ROOT-MONYYFUT C/P strike


def live_futures(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Filter futures contracts to only those not yet expired."""
    if df.empty:
        return df

    def is_live(expiration_ns):
        if not expiration_ns or expiration_ns == 0:
            return True
        exp_dt = datetime.fromtimestamp(expiration_ns / 10**9)
        return exp_dt > as_of

    return df[df["expiration"].apply(is_live)]


def pick_nearest_expiry(df: pd.DataFrame) -> pd.DataFrame:
    """
    From a group of contracts (same underlying, different expiries),
    select only the nearest-expiry contract.
    """
    if df.empty:
        return df

    # Group by underlying_root and find minimum expiration
    result = []
    for underlying, group in df.groupby("underlying_root"):
        live = group[group["expiration"] > 0]
        if live.empty:
            live = group

        min_exp_idx = live["expiration"].idxmin()
        result.append(df.loc[min_exp_idx])

    return pd.DataFrame(result) if result else df


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


def build_contract_index(as_of: str) -> Dict[str, dict]:
    """Map contract script -> full contract row, built once per run and shared
    across every basket refresh (avoids re-reading all normalized CSVs per basket)."""
    return {row["script"]: row for row in export.aggregate_contract_rows(as_of) if row.get("script")}


def refresh_basket(name: str, as_of: str, contract_index: Dict[str, dict], dry_run: bool = False) -> Optional[pd.DataFrame]:
    """
    Refresh a single basket: look up each listed contract script in today's
    normalized contracts. Returns DataFrame of matched contract rows, or None
    if the basket file is missing/empty or none of its constituents matched.
    """
    basket_file = paths.baskets_dir() / f"{name}.csv"
    if not basket_file.exists():
        print(f"    Basket {name} not found at {basket_file}")
        return None

    scripts = _load_basket_scripts(basket_file)
    if not scripts:
        print(f"    Basket {name} is empty")
        return None

    rows = []
    missing = 0
    for script in scripts:
        row = contract_index.get(script)
        if row is None:
            missing += 1
            continue
        rows.append(row)

    if missing:
        print(f"    {name}: {missing}/{len(scripts)} constituents not found in today's contracts")

    return pd.DataFrame(rows) if rows else None


def refresh_all(as_of: str, normalized_dir: Path, dry_run: bool = False) -> None:
    """Refresh all baskets for a given day."""
    contracts_day_dir = paths.contracts_day_dir(as_of)
    contracts_day_dir.mkdir(parents=True, exist_ok=True)

    contract_index = build_contract_index(as_of)

    for basket_name in paths.BASKET_NAMES:
        if dry_run:
            print(f"    Would refresh basket {basket_name}")
            continue

        try:
            df = refresh_basket(basket_name, as_of, contract_index, dry_run)
            if df is not None and not df.empty:
                output_csv = contracts_day_dir / f"{basket_name}.csv"
                df.to_csv(output_csv, index=False, encoding="utf-8-sig")
                print(f"    Wrote {basket_name}: {len(df)} contracts")
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
            contract_index = build_contract_index(opts.date_dir)
            df = refresh_basket(opts.basket, opts.date_dir, contract_index, opts.dry_run)
            if df is not None:
                contracts_dir = paths.contracts_day_dir(opts.date_dir)
                contracts_dir.mkdir(parents=True, exist_ok=True)
                output_csv = contracts_dir / f"{opts.basket}.csv"
                df.to_csv(output_csv, index=False, encoding="utf-8-sig")
                print(f"    Wrote {opts.basket}: {len(df)} rows")
        except Exception as e:
            print(f"    Error refreshing {opts.basket}: {e}")
    else:
        # All baskets
        refresh_all(opts.date_dir, normalized_dir, opts.dry_run)
