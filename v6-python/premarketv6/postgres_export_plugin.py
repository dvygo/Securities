"""Postgres plugin-table appender: push data/YYYYMMDD/v6/plugin/*.parquet (legacy
pg symbol-master schema, see docs/plugin/pg_data_types.txt) into an existing,
externally-managed Postgres table.

Unlike postgres_export.py's contracts/baskets tables (DROP+CREATE, owned by
this pipeline), the plugin table's DDL lives outside this repo -- this only
COPY-appends rows into it, restricted to the [postgres-plugin] exchanges
allow-list (MIC filename prefix, e.g. XNSE/XIMC/XBOM/XNAS). It creates the
table only when [postgres-plugin] create_table = 1, which defaults to 0; see
_ensure_table for why that default matters.

The target table's primary key is (token, trade_date), with no exchange column
to separate two venues that picked the same number. The token pushed is
counterTokenV2 (normalize/counter_token.py), which carries the venue's own
two-digit prefix in its leading digits precisely so that cannot happen: on
2026-08-26 all 3,017,990 rows across six venues were distinct.

That is a change of source, not only of value. The token used to be the bare
Databento instrument_id, which is unique only within a dataset -- XCME token
81352 was seen colliding with an unrelated pre-existing row. Rows pushed before
that change sit in a different number space from rows pushed after it, so they
do not collide with each other either.
"""
import csv
import io
from typing import List

import psycopg

from . import config, parquet_export, paths, runner
from .normalize import plugin as plugin_norm

# Column types for the plugin table, from docs/plugin/pg_data_types.txt.
# Keyed by column name and rendered in plugin_norm.PLUGIN_COLUMNS order, so a
# column added there without a type here fails loudly at _create_table_sql
# rather than producing a table quietly missing it.
PLUGIN_COLUMN_TYPES = {
    "trade_date":  "date NOT NULL DEFAULT CURRENT_DATE",
    "segment":     "varchar(15)",
    "token":       "int8 NOT NULL DEFAULT 0",
    "symbol":      "varchar(255)",
    "expirydate":  "varchar(11)",
    "insttype":    "varchar(255)",
    "optiontype":  "varchar(255)",
    "strikeprice": "int8",
    "lotmultiple": "float8",
    "lotsize":     "int4",
    "ticksize":    "float8 DEFAULT 0",
    "name":        "varchar(255)",
    "series":      "varchar(255)",
    "divisor":     "int4 NOT NULL DEFAULT 0",
    "exch":        "varchar(10)",
    "fullname":    "varchar(100)",
    "freeze_qty":  "int4",
}

# The plugin table keys on (token, trade_date) -- no exchange column, which is
# why counterToken/counterTokenV2 exist to keep a token unique across venues.
PLUGIN_PRIMARY_KEY = ("token", "trade_date")


def _create_table_sql(schema: str, table: str) -> str:
    """DDL for the plugin table, in PLUGIN_COLUMNS order."""
    missing = [c for c in plugin_norm.PLUGIN_COLUMNS if c not in PLUGIN_COLUMN_TYPES]
    if missing:
        raise ValueError(
            f"No Postgres type for plugin column(s): {', '.join(missing)}. "
            f"Add them to PLUGIN_COLUMN_TYPES (see docs/plugin/pg_data_types.txt)."
        )
    cols = ",\n".join(
        f'    "{c}" {PLUGIN_COLUMN_TYPES[c]}' for c in plugin_norm.PLUGIN_COLUMNS
    )
    pk = ", ".join(f'"{c}"' for c in PLUGIN_PRIMARY_KEY)
    return (
        f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}" (\n'
        f"{cols},\n"
        f"    PRIMARY KEY ({pk})\n"
        f")"
    )


def _table_exists(conn: psycopg.connection.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}"."{table}"',))
        return cur.fetchone()[0] is not None


def _ensure_table(conn: psycopg.connection.Connection, schema: str, table: str,
                  create: bool) -> None:
    """Check the plugin table is there, creating it only when told to.

    The table is externally managed, so the default is to check and complain
    rather than create. "relation does not exist" is a useful error -- it is
    what catches a mistyped schema or table before a single row is written.
    Creating on demand turns that same typo into a new table quietly filling up
    on whatever database the DSN names.

    With create on, everything is IF NOT EXISTS, so an existing table is never
    altered: a real plugin table whose schema differs is left exactly as it is
    rather than migrated behind the operator's back.
    """
    if _table_exists(conn, schema, table):
        return
    if not create:
        raise RuntimeError(
            f'relation "{schema}.{table}" does not exist. Check '
            f"[postgres-plugin] schema/table in config.ini -- this is the error "
            f"that catches a typo. If the database is genuinely new and the "
            f"table should be created, set create_table = 1 in that section."
        )
    print(f"    Creating {schema}.{table} (create_table = 1)")
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(_create_table_sql(schema, table))
    conn.commit()


def _batch_csv(columns: List[str], rows: List[dict], header: bool) -> str:
    """One batch of rows as CSV text. The header goes on the first batch only."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    if header:
        writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) in (None, "") else row.get(c) for c in columns])
    return buf.getvalue()


def _copy_append(conn: psycopg.connection.Connection, schema: str, table: str,
                 columns: List[str], batches) -> int:
    """COPY-append batches of rows into schema.table (no DROP/CREATE -- the table exists).

    Takes an iterable of row batches rather than one list, and feeds them into a
    single COPY as they arrive. The plugin build is chunked precisely so an
    --all-symbols venue is never held whole; reading it back with read_rows put
    it straight back into memory. Measured on the 2026-08-26 OPRA file: 1,636
    bytes per row as dicts, so 1,998,042 rows is ~3.3 GB before a single byte
    reaches Postgres.

    Still one COPY and one commit per file, so a failure mid-file rolls the
    whole file back exactly as it did before. Streaming changes the memory
    profile, not the transaction boundary.
    """
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    copy_sql = f'COPY "{schema}"."{table}" ({quoted_cols}) FROM STDIN WITH (FORMAT csv, HEADER true)'

    total = 0
    with conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            for rows in batches:
                if not rows:
                    continue
                copy.write(_batch_csv(columns, rows, header=(total == 0)))
                total += len(rows)
        if total == 0:
            # Nothing was written, so the COPY saw only a header-less empty
            # stream. Roll back rather than commit an empty transaction.
            conn.rollback()
            return 0
    conn.commit()
    return total


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

    # A disabled venue never reaches the table, even if a plugin file for it is
    # still on disk from the last run it was enabled for. The allow-list below
    # is a separate, narrower filter: enabled says whether the pipeline runs the
    # venue at all, [postgres-plugin].exchanges says which of the venues it does
    # run get pushed to this particular table.
    exchanges = config.load_exchanges()
    disabled = {c.venue_name for c in exchanges.values() if not c.enabled}
    if disabled:
        skipped = [p for p in plugin_files if p.name.split("-", 1)[0] in disabled]
        for path in skipped:
            print(f"  Skipping {path.name}: {path.name.split('-', 1)[0]} enabled = 0")
        plugin_files = [p for p in plugin_files if p.name.split("-", 1)[0] not in disabled]

    if cfg.exchanges:
        plugin_files = [p for p in plugin_files if p.name.split("-", 1)[0] in cfg.exchanges]

    if not plugin_files:
        print("  No plugin files matched the configured exchange allow-list")
        return

    print(f"  Appending to {cfg.schema}.{cfg.table}...")
    try:
        with psycopg.connect(cfg.database_url) as conn:
            _ensure_table(conn, cfg.schema, cfg.table, cfg.create_table)
            for path in plugin_files:
                n = _copy_append(conn, cfg.schema, cfg.table, plugin_norm.PLUGIN_COLUMNS,
                                 parquet_export.iter_rows(path))
                print(f"    Appended {n} rows from {path.name}")
    except Exception as e:
        print(f"  Error appending to Postgres: {e}")
        raise
