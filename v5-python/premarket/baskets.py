"""Basket/constituents resolution: build daily contract lists."""
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd

from . import paths, runner


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


def refresh_basket(name: str, as_of: str, normalized_dir: Path, dry_run: bool = False) -> Optional[pd.DataFrame]:
    """
    Refresh a single basket: resolve underlying symbols to concrete contracts.
    Returns DataFrame of contract symbols for the basket, or None if not available.
    """
    basket_file = paths.baskets_dir() / f"{name}.csv"
    if not basket_file.exists():
        print(f"    Basket {name} not found at {basket_file}")
        return None

    # Read basket membership (list of underlying symbols)
    basket_df = pd.read_csv(basket_file)
    if basket_df.empty:
        print(f"    Basket {name} is empty")
        return None

    # Resolve each underlying to concrete contracts from normalized data
    results = []
    as_of_dt = datetime.strptime(as_of, "%Y%m%d")

    for _, row in basket_df.iterrows():
        underlying = row.get("symbol") or row.get("underlying", "")
        if not underlying:
            continue

        # Find matching contracts in normalized data
        # This is simplified; actual implementation would join with normalized CSVs
        contract_row = {
            "date": as_of,
            "symbol": underlying,
            "underlying": underlying,
        }
        results.append(contract_row)

    return pd.DataFrame(results) if results else None


def refresh_all(as_of: str, normalized_dir: Path, dry_run: bool = False) -> None:
    """Refresh all baskets for a given day."""
    contracts_day_dir = paths.contracts_day_dir(as_of)
    contracts_day_dir.mkdir(parents=True, exist_ok=True)

    for basket_name in paths.BASKET_NAMES:
        if dry_run:
            print(f"    Would refresh basket {basket_name}")
            continue

        try:
            df = refresh_basket(basket_name, as_of, normalized_dir, dry_run)
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
            df = refresh_basket(opts.basket, opts.date_dir, normalized_dir, opts.dry_run)
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
