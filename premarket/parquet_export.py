"""Parquet is the pipeline's on-disk format for normalized and plugin output.

Replaces the CSV-plus-.bin-companion arrangement this used to have, where every
stage wrote a CSV and then re-read the finished file to produce a Parquet copy
named .bin. That cost a second full pass and left two artefacts disagreeing
about nothing. Rows now go straight to Parquet and the CSV never exists.

Measured on 2026-08-13 OPRA: the CSV was 396.9 MB and its Parquet ~6.6x smaller,
about 3x faster to write than gzip at level 6. The trades and definition
downloads land the same way -- zstd took a 201 GB definition year to 14 GB.

Every column is written as a string, deliberately. These files are a normalized
view of vendor text, and inferring types here would mean this module holding an
opinion about what a blank cell means -- pandas' inference is exactly what used
to render instrument ids as "637543226.0". A consumer casts what it needs, from
a value identical to what the vendor sent.

Readers get row dicts of str, which is what the CSV path handed them (pandas
read_csv with dtype=str, keep_default_na=False), so the stages consuming these
files did not have to change how they treat a value.
"""
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

# Rows per Parquet row group, and the batch size on the way back in. Bounds the
# memory a conversion holds regardless of how large the file is.
CHUNK_ROWS = 200_000

COMPRESSION = "zstd"

SUFFIX = ".parquet"


def _schema(columns: Sequence[str]) -> pa.Schema:
    """All-string schema, in the given column order."""
    return pa.schema([(name, pa.string()) for name in columns])


class RowWriter:
    """Streaming Parquet writer taking batches of row dicts.

    Staged under a PID-scoped temp name and promoted on close(), for the reason
    every other writer here does it: two runs must not share a path, and readers
    must never see a partial file. Parquet writes its footer last, so a killed
    run leaves something unreadable rather than silently short -- but a
    half-written file under the real name would still fail whatever picks it up.

    close() on a writer that received no rows removes the staging file and
    leaves no output at all. A row-group-less Parquet file is a trap for
    readers, and an empty venue should look absent, not empty.
    """

    def __init__(self, path: Path, columns: Sequence[str]):
        self.path = Path(path)
        self.columns = list(columns)
        self._schema_ = _schema(self.columns)
        self._temp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        self._writer: Optional[pq.ParquetWriter] = None
        self.total = 0

    def write(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        # str() rather than trusting the caller: a mapper that returns an int for
        # lotSize would otherwise fail the schema mid-run, after the file is part
        # written. "" for a missing key matches what DictWriter's restval did.
        arrays = [
            pa.array(["" if r.get(c) is None else str(r.get(c, "")) for r in rows], type=pa.string())
            for c in self.columns
        ]
        table = pa.Table.from_arrays(arrays, schema=self._schema_)
        if self._writer is None:
            self._temp.parent.mkdir(parents=True, exist_ok=True)
            self._writer = pq.ParquetWriter(self._temp, self._schema_, compression=COMPRESSION)
        self._writer.write_table(table)
        self.total += len(rows)

    def close(self) -> bool:
        """Promote the staged file. Returns False if nothing was written."""
        if self._writer is None:
            self._temp.unlink(missing_ok=True)
            return False
        self._writer.close()
        self._writer = None
        os.replace(self._temp, self.path)
        return True

    def abort(self) -> None:
        """Discard a part-written file, for a failed run."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._temp.unlink(missing_ok=True)

    def __enter__(self) -> "RowWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.abort()
        else:
            self.close()


def write_rows(path: Path, columns: Sequence[str], rows: List[Dict[str, Any]]) -> bool:
    """Write a whole list of rows in one call. Returns False if rows was empty."""
    w = RowWriter(path, columns)
    for i in range(0, len(rows), CHUNK_ROWS):
        w.write(rows[i:i + CHUNK_ROWS])
    return w.close()


def iter_rows(path: Path, batch_rows: int = CHUNK_ROWS) -> Iterator[List[Dict[str, str]]]:
    """Yield batches of row dicts from a Parquet file.

    Values come back as str, matching what read_csv(dtype=str,
    keep_default_na=False) produced, so a blank cell is "" and never NaN.
    """
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_rows):
        yield [
            {k: ("" if v is None else v) for k, v in row.items()}
            for row in batch.to_pylist()
        ]


def read_rows(path: Path) -> List[Dict[str, str]]:
    """Every row of a Parquet file as dicts. Use iter_rows for large files."""
    return [row for batch in iter_rows(path) for row in batch]


def row_count(path: Path) -> int:
    """Rows in a Parquet file, from the footer -- no data is read."""
    return pq.ParquetFile(path).metadata.num_rows
