"""Compressed .bin companions for the normalized and plugin CSVs.

Each finished CSV gets a sibling <name>.bin holding the same rows as Parquet with
zstd compression. Measured on 2026-08-13 OPRA (396.9 MB of CSV): ~6.6x smaller and
about 3x faster to write than gzip at level 6, which was the alternative.

Every column is written as a string, deliberately. The CSVs are the canonical
artefact and are themselves untyped text, so inferring types here would mean the
.bin and the .csv disagree about what a blank cell is -- and pandas' inference is
exactly what used to render instrument ids as "637543226.0". A consumer that wants
numbers casts them itself, from a value identical to the one in the CSV.

The conversion re-reads the finished CSV in chunks rather than tapping the write
path. That costs one extra pass, but it means a single implementation serves all
three producers (databento_norm and plugin stream their rows, fields builds a
frame), and it can never emit a .bin for a CSV that failed to finish.
"""
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import paths

# Rows per Parquet row group. Also the read chunk, so this bounds the memory the
# conversion holds regardless of how large the source file is.
BIN_CHUNK_ROWS = 200_000

BIN_COMPRESSION = "zstd"


def companion_path(csv_path: Path) -> Path:
    """Path of the .bin sibling for a CSV."""
    return csv_path.with_suffix(".bin")


def write_companion(csv_path: Path, chunk_rows: int = BIN_CHUNK_ROWS) -> Optional[Path]:
    """Stream a finished CSV into a compressed .bin beside it.

    Returns the .bin path, or None for an empty/header-only CSV -- a Parquet file
    with no row groups is a trap for readers, and there is nothing to compress.
    Raises on a genuine conversion failure; the caller decides whether a missing
    .bin should fail the step.
    """
    out_path = companion_path(csv_path)
    # Staged like every other output here so a killed run cannot leave a truncated
    # .bin that looks complete. Parquet writes its footer last, so a partial file
    # is unreadable rather than silently short -- but a half-written file sitting
    # under the real name would still fail whatever picks it up next.
    temp_path = out_path.with_name(f"{out_path.name}.tmp.{os.getpid()}")

    writer = None
    total = 0
    try:
        for frame in pd.read_csv(
            csv_path, dtype=str, keep_default_na=False, chunksize=chunk_rows
        ):
            table = pa.Table.from_pandas(frame, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(temp_path, table.schema, compression=BIN_COMPRESSION)
            writer.write_table(table)
            total += len(frame)
    except Exception:
        if writer is not None:
            writer.close()
        temp_path.unlink(missing_ok=True)
        raise

    if writer is None:
        return None  # header-only CSV: nothing to write
    writer.close()

    paths.promote_staging(temp_path, out_path)
    return out_path


def write_companion_safe(csv_path: Path) -> Optional[Path]:
    """write_companion, but a failure warns instead of failing the whole step.

    The CSV is the canonical artefact and is already on disk by this point. A
    .bin that could not be written is a missing convenience, not a lost run, so
    it must not take the pipeline down with it.
    """
    try:
        out = write_companion(csv_path)
    except Exception as e:
        print(f"      Warning: could not write {companion_path(csv_path).name}: {e}")
        return None
    if out is not None:
        src_mb = csv_path.stat().st_size / 1e6
        out_mb = out.stat().st_size / 1e6
        print(f"      Wrote {out.name} ({out_mb:.1f} MB, {src_mb / out_mb:.1f}x smaller)")
    return out
