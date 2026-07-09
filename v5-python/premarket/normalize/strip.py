"""Strip filter: near-term OPRA contract prefilter."""
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .. import paths, runner


def run(opts: runner.Opts) -> None:
    """Filter OPRA symbology to near-term contracts only (within 14 days for SPXW)."""
    if opts.dry_run:
        print("DRY RUN: Would strip near-term OPRA contracts")
        return

    print("  Stripping near-term OPRA contracts...")
    normalized_dir = paths.normalized_dir(opts.date_dir)

    # Find OPRA/XCBO normalized CSV
    opra_csv = normalized_dir / "databento_xcbo_hist_normalized.csv"
    if not opra_csv.exists():
        print("    No OPRA data to strip")
        return

    try:
        df = pd.read_csv(opra_csv)

        # Filter to near-term contracts (within 14 days)
        today = datetime.now()
        cutoff_date = today + timedelta(days=14)

        def is_near_term(expiration_ns):
            if not expiration_ns or expiration_ns == 0:
                return True  # Keep if no expiration
            exp_dt = datetime.fromtimestamp(expiration_ns / 10**9)
            return exp_dt <= cutoff_date

        # Apply filter
        df["_near_term"] = df["expiration"].apply(is_near_term)
        filtered_df = df[df["_near_term"]].drop("_near_term", axis=1)

        # Write stripped CSV
        stripped_csv = opra_csv.with_suffix(".stripped.csv")
        filtered_df.to_csv(stripped_csv, index=False, encoding="utf-8-sig")
        print(f"    Filtered {len(df)} to {len(filtered_df)} near-term contracts")
    except Exception as e:
        print(f"    Error stripping OPRA data: {e}")
