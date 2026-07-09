"""NSE normalization: raw passthrough (no transformation)."""
import shutil
from pathlib import Path

from .. import paths, runner


def run(opts: runner.Opts) -> None:
    """
    Copy NSE raw exchange files to normalized directory.
    NSE files are not transformed, just byte-copied (different schema).
    """
    if opts.dry_run:
        print("DRY RUN: Would copy NSE raw files")
        return

    print("  Copying NSE exchange files...")

    nse_raw_dir = paths.nse_exchange_raw_dir(opts.date_dir)
    normalized_dir = paths.normalized_dir(opts.date_dir)

    if not nse_raw_dir.exists():
        print("    No NSE raw directory found")
        return

    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Copy each NSE segment file
    for segment_name, filename in paths.NSE_SEGMENTS.items():
        src = nse_raw_dir / filename
        if src.exists():
            dst = normalized_dir / f"NSE-{segment_name}.csv"
            try:
                shutil.copy2(src, dst)
                print(f"    Copied {filename}")
            except Exception as e:
                print(f"    Error copying {filename}: {e}")
        else:
            print(f"    {filename} not found (skipping)")
