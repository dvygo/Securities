"""CSV export: aggregate normalized data to contracts.csv and baskets.csv."""
from pathlib import Path
from typing import List

import pandas as pd

from . import paths, runner


def aggregate_contract_rows(date_dir: str) -> List[dict]:
    """
    Aggregate all normalized symbol contracts into flat contract rows.
    Returns list of dicts with [date, exchange] + normalized columns.
    """
    normalized_dir = paths.normalized_dir(date_dir)
    rows = []

    if not normalized_dir.exists():
        return rows

    # Find all normalized CSVs (Fyers, Databento, NSE)
    csv_files = normalized_dir.glob("*.csv")

    for csv_path in csv_files:
        # Skip already-stripped files and NSE raw files
        if csv_path.name.endswith(".stripped.csv") or csv_path.name.startswith("NSE-"):
            continue

        # Normalized filenames are always "{MIC}-...csv" (XNSE-FYERS.csv,
        # XCME-DATABENTO-normalized.csv, ...); the 16-col schema never
        # carries an exchange column itself, so derive it from the name
        # like v4-golang's ExchangeMICForNormalizedCSV.
        exchange = csv_path.name.split("-", 1)[0]

        try:
            df = pd.read_csv(csv_path)
            for _, row in df.iterrows():
                contract_row = {
                    "date": date_dir,
                    "exchange": exchange,
                }
                # Add canonical columns
                for col in paths.NORMALIZED_COLUMNS:
                    contract_row[col] = row.get(col, "")

                # Filter out invalid rows
                if contract_row.get("scriptToken") and contract_row.get("exchange"):
                    rows.append(contract_row)
        except Exception as e:
            print(f"  Error reading {csv_path}: {e}")

    # Deduplicate by (scriptToken, script)
    seen = set()
    deduped = []
    for row in rows:
        key = (row.get("scriptToken"), row.get("script"))
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    return deduped


def aggregate_basket_rows(date_dir: str) -> List[dict]:
    """
    Aggregate all basket constituents into flat basket rows.
    Returns list of dicts with [date, basket, symbol].
    """
    contracts_day_dir = paths.contracts_day_dir(date_dir)
    rows = []

    if not contracts_day_dir.exists():
        return rows

    # Find all basket CSV files
    for csv_path in contracts_day_dir.glob("*.csv"):
        basket_name = csv_path.stem
        try:
            df = pd.read_csv(csv_path)
            for _, row in df.iterrows():
                basket_row = {
                    "date": date_dir,
                    "basket": basket_name,
                    "symbol": row.get("symbol", "") or row.get("script", ""),
                }
                if basket_row.get("symbol"):
                    rows.append(basket_row)
        except Exception as e:
            print(f"  Error reading {csv_path}: {e}")

    return rows


def write_aggregate_csvs(
    date_dir: str,
    output_dir: Path,
    export_contracts: bool = True,
    export_baskets: bool = True,
) -> None:
    """Write aggregated CSV exports to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if export_contracts:
        print("  Exporting contracts.csv...")
        contract_rows = aggregate_contract_rows(date_dir)
        if contract_rows:
            df = pd.DataFrame(contract_rows)
            # Reorder columns: date, exchange + normalized
            cols = ["date", "exchange"] + paths.NORMALIZED_COLUMNS
            df = df[cols]
            output_csv = output_dir / "contracts.csv"
            df.to_csv(output_csv, index=False, encoding="utf-8-sig")
            print(f"    Wrote {len(df)} rows to {output_csv}")

    if export_baskets:
        print("  Exporting baskets.csv...")
        basket_rows = aggregate_basket_rows(date_dir)
        if basket_rows:
            df = pd.DataFrame(basket_rows)
            output_csv = output_dir / "baskets.csv"
            df.to_csv(output_csv, index=False, encoding="utf-8-sig")
            print(f"    Wrote {len(df)} rows to {output_csv}")


def run(opts: runner.Opts) -> None:
    """Export aggregated CSVs."""
    if opts.dry_run:
        print("DRY RUN: Would export CSVs")
        return

    print("  Exporting CSVs...")

    # If --csv dir specified, write there; otherwise skip CSV export
    if not hasattr(opts, "csv_export_dir") or not opts.csv_export_dir:
        print("    No --csv directory specified, skipping CSV export")
        return

    output_dir = Path(opts.csv_export_dir)
    write_aggregate_csvs(opts.date_dir, output_dir)
