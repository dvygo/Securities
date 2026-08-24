"""Postgres plugin-table appender: push data/YYYYMMDD/v6/plugin/*.csv (legacy pg
symbol-master schema, see docs/plugin/pg_data_types.txt) into an existing,
externally-managed Postgres table.

Unlike postgres_export.py's contracts/baskets tables (DROP+CREATE, owned by
this pipeline), the plugin table's DDL lives outside this repo -- this only
COPY-appends rows into it, restricted to the [postgres-plugin] exchanges
allow-list (MIC filename prefix, e.g. XNSE/XIMC/XBOM/XNAS).

The target table's primary key is (token, trade_date) with no exchange
column, so a Databento instrument_id can collide with an unrelated token
already sitting in that table (seen live: XCME token 81352 collided with a
pre-existing row). This used to be handled by prepending a namespace prefix to
every pushed token; that prefixing has been removed by request, so tokens are
pushed as the bare instrument_id and that collision is possible again --
both against pre-existing rows and between two venues pushed on the same
trade_date, since Databento only guarantees instrument_id is unique within a
dataset.
"""
import csv
import io
from typing import List

import psycopg

from . import config, parquet_export, paths, runner
from .normalize import plugin as plugin_norm

# Token namespacing removed by request. Tokens are now pushed as the bare
# Databento instrument_id, so they can collide with pre-existing rows in the
# externally-managed table again (see module docstring for the observed case).


def _copy_append(conn: psycopg.connection.Connection, schema: str, table: str, columns: List[str], rows: List[dict]) -> int:
    """COPY-append rows into schema.table via real CSV format (no DROP/CREATE -- the table already exists)."""
    if not rows:
        return 0

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) in (None, "") else row.get(c) for c in columns])
    buf.seek(0)

    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    copy_sql = f'COPY "{schema}"."{table}" ({quoted_cols}) FROM STDIN WITH (FORMAT csv, HEADER true)'

    with conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            copy.write(buf.read())
    conn.commit()
    return len(rows)


def run(opts: runner.Opts) -> None:
    """Append every allow-listed plugin CSV for the day to the configured Postgres table."""
    if opts.dry_run:
        print("DRY RUN: Would append plugin CSVs to Postgres")
        return

    cfg = config.load_postgres_plugin()
    if not cfg.database_url:
        print("  Error: [postgres-plugin].database_url not configured (config.ini or DATABASE_URL_PLUGIN)")
        return
    if not cfg.schema or not cfg.table:
        print("  Error: [postgres-plugin] schema/table not configured")
        return

    plugin_dir = paths.plugin_dir(opts.date_dir)
    if not plugin_dir.exists():
        print(f"  No plugin dir for {opts.date_dir} -- run normalize --plugin first")
        return

    plugin_files = sorted(plugin_dir.glob(f"*{parquet_export.SUFFIX}"))
    if cfg.exchanges:
        plugin_files = [p for p in plugin_files if p.name.split("-", 1)[0] in cfg.exchanges]

    if not plugin_files:
        print("  No plugin files matched the configured exchange allow-list")
        return

    print(f"  Appending to {cfg.schema}.{cfg.table}...")
    try:
        with psycopg.connect(cfg.database_url) as conn:
            for path in plugin_files:
                rows = parquet_export.read_rows(path)
                n = _copy_append(conn, cfg.schema, cfg.table, plugin_norm.PLUGIN_COLUMNS, rows)
                print(f"    Appended {n} rows from {path.name}")
    except Exception as e:
        print(f"  Error appending to Postgres: {e}")
        raise
