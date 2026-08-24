"""CSV export: aggregate normalized data to contracts.csv and baskets.csv."""
from pathlib import Path
from typing import List

import pandas as pd

from . import parquet_export, paths, runner


def normalized_files(date_dir: str) -> List[Path]:
    """
    List eligible normalized Parquet files (Fyers, Databento) for a day.

    NSE raw passthrough files are excluded by extension now rather than by their
    "NSE-" prefix: nse_norm.py byte-copies vendor CSVs under a different,
    non-canonical schema, so they are not Parquet and the glob never sees them.
    The prefix check is kept anyway, since a future passthrough could arrive in
    any format and it costs nothing.
    """
    normalized_dir = paths.normalized_dir(date_dir)
    if not normalized_dir.exists():
        return []
    return [
        p for p in normalized_dir.glob(f"*{parquet_export.SUFFIX}")
        if not p.name.startswith("NSE-")
    ]


# Rows per yielded batch. Matches the chunk size the download and normalize
# stages already use, and is the unit the ClickHouse push inserts in.
CONTRACT_BATCH_ROWS = 50_000


def iter_contract_rows(date_dir: str, batch_rows: int = CONTRACT_BATCH_ROWS):
    """Yield [date, exchange] + normalized-column rows in batches.

    Streams the normalized CSVs rather than materialising every row: an
    --all-symbols GLBX day is ~1.09M contracts, which the list-returning version
    below holds as ~1.09M dicts, then again inside whatever consumes it.

    Dedupe on (scriptToken, script) is unchanged and still global, so the `seen`
    set is the one thing held for the whole walk -- keys only, not rows, the same
    trade the definition download makes.
    """
    seen = set()
    for path in normalized_files(date_dir):
        # Normalized filenames are always "{MIC}-...parquet" (XNSE-FYERS.parquet,
        # XCME-DATABENTO-normalized.parquet, ...); the schema never carries an
        # exchange column itself, so derive it from the name.
        exchange = path.name.split("-", 1)[0]
        batch = []
        try:
            for rows in parquet_export.iter_rows(path, batch_rows):
                for row in rows:
                    if not row.get("scriptToken"):
                        continue
                    key = (row.get("scriptToken"), row.get("script"))
                    if key in seen:
                        continue
                    seen.add(key)
                    contract_row = {"date": date_dir, "exchange": exchange}
                    for col in paths.NORMALIZED_COLUMNS:
                        contract_row[col] = row.get(col, "")
                    batch.append(contract_row)
                    if len(batch) >= batch_rows:
                        yield batch
                        batch = []
        except Exception as e:
            print(f"  Error reading {path}: {e}")
        if batch:
            yield batch


def aggregate_contract_rows(date_dir: str) -> List[dict]:
    """
    Aggregate all normalized symbol contracts into flat contract rows.
    Returns list of dicts with [date, exchange] + normalized columns.

    Materialises what iter_contract_rows streams. Kept for the CSV and SQLite
    exports, which build a DataFrame from the whole day anyway; the database
    pushes should use the iterator instead.
    """
    return [row for batch in iter_contract_rows(date_dir) for row in batch]


def aggregate_basket_rows(date_dir: str) -> List[dict]:
    """
    Aggregate all basket constituents into flat basket rows.
    Returns list of dicts with [date, basket] + Nexus's basket columns
    (paths.NEXUS_BASKET_COLUMNS) wherever the per-basket file has them --
    the "script" fallback covers today's stub baskets.py output, which
    only ever writes [date, symbol, underlying].
    """
    contracts_day_dir = paths.contracts_day_dir(date_dir)
    rows = []

    if not contracts_day_dir.exists():
        return rows

    for path in contracts_day_dir.glob(f"*{parquet_export.SUFFIX}"):
        basket_name = path.stem
        try:
            for row in parquet_export.read_rows(path):
                script = row.get("script", "") or row.get("symbol", "")
                if not script:
                    continue
                basket_row = {"date": date_dir, "basket": basket_name, "script": script}
                for col in paths.NEXUS_BASKET_COLUMNS:
                    if col == "script":
                        continue
                    basket_row[col] = row.get(col, "")
                rows.append(basket_row)
        except Exception as e:
            print(f"  Error reading {path}: {e}")

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
