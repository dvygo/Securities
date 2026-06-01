#!/usr/bin/env python3
"""Load normalized symbology CSVs into Postgres primary (schema per day: ``v2-YYYYMMDD``).

US tables: ``equs_mini``, ``glbx_mdp3``, ``opra_pillar``.
India tables: ``nse_cm``, ``nse_fo``, ``nse_cd``, ``bse_cm``, ``bse_fo``, ``mcx_com``.
Replace mode: DROP TABLE IF EXISTS → CREATE → COPY.

  python postgres-database-push.py
  python postgres-database-push.py --date-dir 20260521
  python postgres-database-push.py --dry-run

Uses ``DATABASE_URL`` or ``[postgres] database_url`` in config.ini (port **7710** primary).
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from datetime import date
from pathlib import Path

import psycopg

from symbology_paths import (
    FYERS_SEGMENTS,
    NORMALIZED_SUBDIR,
    XCBO_CSV,
    XCME_CSV,
    XNAS_CSV,
    postgres_schema,
    repo_root,
)

_V4_DIR = repo_root()
_DATE_DIR_RE = re.compile(r"^\d{8}$")
_POSTGRES_SCHEMA_RE = re.compile(r"^v2-\d{8}$")

TABLES: tuple[tuple[str, str], ...] = (
    ("equs_mini", XNAS_CSV),
    ("glbx_mdp3", XCME_CSV),
    ("opra_pillar", XCBO_CSV),
    *((seg.postgres_table, seg.output_csv) for seg in FYERS_SEGMENTS),
)

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("date", "INTEGER NOT NULL"),
    ("exchange", "TEXT"),
    ("underlying_root", "TEXT"),
    ("underlying", "TEXT"),
    ("strike", "BIGINT"),
    ("expiration", "BIGINT"),
    ("multiplier", "BIGINT"),
    ("token", "BIGINT NOT NULL"),
    ("symbol", "TEXT"),
)

_COL_NAMES: tuple[str, ...] = tuple(c[0] for c in _COLUMNS)
_NULLABLE_BIGINT_COLS = frozenset({"strike", "expiration", "multiplier"})
_DATE_RE_ROW = re.compile(r"^\d{8}$")


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema_ddl(schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {_qident(schema)};"


def _create_table_ddl(schema: str, table: str) -> str:
    sch, tbl = _qident(schema), _qident(table)
    cols = ",\n    ".join(f"{c} {t}" for c, t in _COLUMNS)
    return f"""
DROP TABLE IF EXISTS {sch}.{tbl} CASCADE;
CREATE TABLE {sch}.{tbl} (
    {cols}
);
"""


def _index_ddl(schema: str, table: str) -> str:
    sch, tbl = _qident(schema), _qident(table)
    p = f"{table}"
    return f"""
CREATE UNIQUE INDEX IF NOT EXISTS {p}_token_symbol_uq
    ON {sch}.{tbl} (token, symbol);
CREATE INDEX IF NOT EXISTS {p}_underlying_expiration_idx
    ON {sch}.{tbl} (underlying, expiration);
CREATE INDEX IF NOT EXISTS {p}_exchange_underlying_idx
    ON {sch}.{tbl} (exchange, underlying);
CREATE INDEX IF NOT EXISTS {p}_strike_idx
    ON {sch}.{tbl} (strike);
CREATE INDEX IF NOT EXISTS {p}_symbol_idx
    ON {sch}.{tbl} (symbol);
"""


def _copy_sql(schema: str, table: str) -> str:
    sch, tbl = _qident(schema), _qident(table)
    col_list = ", ".join(_COL_NAMES)
    force_null = ", ".join(sorted(_NULLABLE_BIGINT_COLS))
    return (
        f"COPY {sch}.{tbl} ({col_list}) "
        f"FROM STDIN WITH (FORMAT csv, HEADER true, ENCODING 'UTF8', FORCE_NULL ({force_null}))"
    )


def _row_ok_for_load(row: dict[str, str]) -> bool:
    """Drop mis-aligned symbology-only appends (missing normalized date/token/symbol)."""
    day = (row.get("date") or "").strip()
    if not _DATE_RE_ROW.match(day):
        return False
    tok = (row.get("token") or "").strip()
    sym = (row.get("symbol") or "").strip()
    if not tok or not sym:
        return False
    try:
        if int(tok) <= 0:
            return False
    except ValueError:
        return False
    return True


def _csv_bytes_for_copy(path: Path) -> tuple[bytes, int, int, int]:
    """Dedupe on (token, symbol)."""
    buf = io.StringIO(newline="")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return b"", 0, 0, 0
        w = csv.DictWriter(buf, fieldnames=_COL_NAMES, extrasaction="ignore")
        w.writeheader()
        seen: set[tuple[str, str]] = set()
        n_in = 0
        n_out = 0
        n_skip = 0
        for row in reader:
            n_in += 1
            if not _row_ok_for_load(row):
                n_skip += 1
                continue
            tok = (row.get("token") or "").strip()
            sym = (row.get("symbol") or "").strip()
            key = (tok, sym)
            if key in seen:
                continue
            seen.add(key)
            out = {c: (row.get(c, "") or "").strip() for c in _COL_NAMES}
            w.writerow(out)
            n_out += 1
    return buf.getvalue().encode("utf-8"), n_in, n_out, n_skip


def _grant_sql(schema: str) -> str:
    sch = _qident(schema)
    return f"""
GRANT USAGE ON SCHEMA {sch} TO PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO PUBLIC;
"""


def _count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        return sum(1 for _ in reader)


def _validate_csv(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    if not data.strip():
        return 0
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path}: missing header")
        missing = [c for c in _COL_NAMES if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
    return _count_csv_rows(path)


def _load_table(cur: psycopg.Cursor, schema: str, table: str, csv_path: Path) -> int:
    data, n_in, n_out, n_skip = _csv_bytes_for_copy(csv_path)
    if not data.strip():
        return 0
    if n_skip:
        print(
            f"skip: {n_skip} malformed/un-normalized rows ({csv_path.name})",
            file=sys.stderr,
            flush=True,
        )
    if n_out < n_in:
        print(
            f"dedupe: {n_in} CSV rows -> {n_out} unique on (token, symbol) "
            f"({csv_path.name})",
            file=sys.stderr,
            flush=True,
        )
    cur.execute(_create_table_ddl(schema, table))
    cur.execute(_index_ddl(schema, table))
    with cur.copy(_copy_sql(schema, table)) as copy:
        copy.write(data)
    cur.execute(f"SELECT COUNT(*)::bigint FROM {_qident(schema)}.{_qident(table)}")
    (n,) = cur.fetchone()
    return int(n)


def push_day(
    *,
    day_dir: Path,
    schema: str,
    database_url: str,
    dry_run: bool,
    skip_missing: bool,
) -> int:
    if not _POSTGRES_SCHEMA_RE.match(schema):
        print(f"error: invalid schema {schema!r}; want v2-YYYYMMDD", file=sys.stderr)
        return 2
    if not day_dir.is_dir():
        print(f"error: not found: {day_dir}", file=sys.stderr)
        return 2

    jobs: list[tuple[str, Path]] = []
    for table, csv_name in TABLES:
        csv_path = day_dir / NORMALIZED_SUBDIR / csv_name
        if not csv_path.is_file():
            if skip_missing:
                print(f"skip (missing): {csv_path}", file=sys.stderr)
                continue
            print(f"error: missing {csv_path}", file=sys.stderr)
            return 2
        jobs.append((table, csv_path))

    if not jobs:
        print("error: no tables to load", file=sys.stderr)
        return 2

    if dry_run:
        print(f"dry-run: schema={schema} url={database_url!r}", file=sys.stderr)
        for table, csv_path in jobs:
            n = _validate_csv(csv_path)
            print(f"  {schema}.{table} <- {csv_path.name} ({n} rows)", file=sys.stderr)
        return 0

    total = 0
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(_schema_ddl(schema))
                for table, csv_path in jobs:
                    n_in = _validate_csv(csv_path)
                    if n_in == 0:
                        print(f"skip (empty): {schema}.{table}", file=sys.stderr)
                        continue
                    n = _load_table(cur, schema, table, csv_path)
                    total += n
                    print(
                        f"loaded {n} rows -> \"{schema}\".{table} ({csv_path.name})",
                        file=sys.stderr,
                        flush=True,
                    )
                cur.execute(_grant_sql(schema))
            conn.commit()
    except psycopg.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"done: schema=\"{schema}\" tables={len(jobs)} total_rows={total}",
        file=sys.stderr,
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date-dir",
        default="",
        help="YYYYMMDD folder under v4 (default: today)",
    )
    p.add_argument(
        "--database-url",
        default="",
        help="override DATABASE_URL / config.ini [postgres] database_url",
    )
    p.add_argument("--dry-run", action="store_true", help="validate only, no DB writes")
    p.add_argument(
        "--skip-missing",
        action="store_true",
        help="skip absent CSVs instead of failing",
    )
    args = p.parse_args(argv)

    date_dir = (args.date_dir or "").strip() or date.today().strftime("%Y%m%d")
    if not _DATE_DIR_RE.match(date_dir):
        print(f"error: invalid --date-dir {date_dir!r}; want YYYYMMDD", file=sys.stderr)
        return 2
    try:
        schema = postgres_schema(date_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    day_dir = _V4_DIR / date_dir

    try:
        from config import get_database_url

        database_url = (args.database_url or "").strip() or get_database_url()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return push_day(
        day_dir=day_dir,
        schema=schema,
        database_url=database_url,
        dry_run=args.dry_run,
        skip_missing=args.skip_missing,
    )


if __name__ == "__main__":
    raise SystemExit(main())
