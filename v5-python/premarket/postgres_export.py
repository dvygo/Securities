"""Postgres export: push normalized data via psycopg with real CSV COPY.

Layout, per push:
  v4_YYYYMMDD.contracts        single table, all exchanges (dated snapshot)
  v4_YYYYMMDD.baskets          single table, all baskets (convenience mirror)
  v4_YYYYMMDD_baskets.<name>   one table per basket -- Nexus reads this
                                schema directly (internal/contract-db/
                                baskets/load.go); schema/table shape here
                                is fixed by Nexus's own code, not ours to
                                change unilaterally.
  public.contracts             always-current mirror of the dated contracts
  public.baskets                always-current mirror of the dated baskets

COPY uses real CSV format with FORCE_NULL so Postgres itself turns blank
fields into SQL NULL -- no manual "\\N" sentinel juggling, and no delimiter
collision with values like tradingSessionUTC that legitimately contain "|".
Every table is DROP+CREATE per run, so reruns never hit a stale unique-key
conflict.
"""
import csv
import io
from typing import Dict, Iterable, List

import psycopg
from psycopg import sql

from . import config, export, paths, runner

# Columns that legitimately go blank and must land as SQL NULL, not "".
NULLABLE_CONTRACT_COLUMNS = {
    "scriptInstrumentType2", "lotSize", "tickSize", "ISIN",
    "expiration", "strike", "optionType",
}

CONTRACT_COLUMN_DDL = [
    ('"date"', "TEXT NOT NULL"),
    ('"exchange"', "TEXT NOT NULL"),
    ('"scriptDetails"', "TEXT NOT NULL"),
    ('"scriptInstrumentType"', "TEXT NOT NULL"),
    ('"scriptInstrumentType2"', "TEXT"),
    ('"multiplier"', "BIGINT"),
    ('"lotSize"', "BIGINT"),
    ('"tickSize"', "BIGINT"),
    ('"ISIN"', "TEXT"),
    ('"tradingSessionUTC"', "TEXT NOT NULL"),
    ('"expiration"', "BIGINT"),
    ('"script"', "TEXT NOT NULL"),
    ('"scriptToken"', "BIGINT NOT NULL"),
    ('"underlying_root"', "TEXT NOT NULL"),
    ('"underlying"', "TEXT NOT NULL"),
    ('"strike"', "BIGINT"),
    ('"optionType"', "TEXT"),
    ('"currency"', "TEXT NOT NULL"),
]

# Matches Nexus's basketSelectCols exactly (script, scriptToken,
# scriptInstrumentType2, optionType, underlying_root, underlying, strike,
# expiration, multiplier, currency, exchange).
NEXUS_BASKET_COLUMN_DDL = [
    ('"script"', "TEXT NOT NULL"),
    ('"scriptToken"', "BIGINT NOT NULL"),
    ('"scriptInstrumentType2"', "TEXT"),
    ('"optionType"', "TEXT"),
    ('"underlying_root"', "TEXT"),
    ('"underlying"', "TEXT"),
    ('"strike"', "BIGINT"),
    ('"expiration"', "BIGINT"),
    ('"multiplier"', "BIGINT"),
    ('"currency"', "TEXT"),
    ('"exchange"', "TEXT"),
]

NULLABLE_BASKET_COLUMNS = {
    "scriptInstrumentType2", "optionType", "underlying_root", "underlying",
    "strike", "expiration", "multiplier", "currency", "exchange",
}

# Convenience merged basket table = "basket" name column + Nexus's columns.
MERGED_BASKET_COLUMN_DDL = [('"basket"', "TEXT NOT NULL")] + NEXUS_BASKET_COLUMN_DDL

# Channel downstream apps LISTEN on for public.contracts reloads.
CONTRACTS_NOTIFY_CHANNEL = "contracts_loaded"


def create_schema(conn: psycopg.connection.Connection, schema_name: str) -> None:
    """Create schema if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name)))
    conn.commit()


def _drop_create_table(conn: psycopg.connection.Connection, schema_name: str, table: str, column_ddl) -> None:
    cols_sql = ",\n    ".join(f"{name} {ddl}" for name, ddl in column_ddl)
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema_name}"."{table}" CASCADE')
        cur.execute(f'CREATE TABLE "{schema_name}"."{table}" (\n    {cols_sql}\n)')
    conn.commit()


def _copy_rows(
    conn: psycopg.connection.Connection,
    schema_name: str,
    table: str,
    columns: List[str],
    rows: List[dict],
    nullable_cols: Iterable[str],
) -> int:
    """COPY rows into schema.table via real CSV format; FORCE_NULL turns blank fields into SQL NULL."""
    if not rows:
        return 0

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) in (None, "") else row.get(c) for c in columns])
    buf.seek(0)

    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    force_null = [c for c in columns if c in nullable_cols]
    force_null_clause = f", FORCE_NULL ({', '.join(f'\"{c}\"' for c in force_null)})" if force_null else ""
    copy_sql = (
        f'COPY "{schema_name}"."{table}" ({quoted_cols}) FROM STDIN '
        f"WITH (FORMAT csv, HEADER true{force_null_clause})"
    )

    with conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            copy.write(buf.read())
    conn.commit()
    return len(rows)


def _ensure_contracts_notify_trigger(conn: psycopg.connection.Connection, schema_name: str) -> None:
    """(Re)attach the pg_notify trigger on schema.contracts -- DROP TABLE CASCADE in
    _drop_create_table wipes any trigger the previous push attached, so this must run
    after every reload, not just once."""
    with conn.cursor() as cur:
        cur.execute(f'''
            CREATE OR REPLACE FUNCTION "{schema_name}".notify_contracts_loaded() RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify(
                    '{CONTRACTS_NOTIFY_CHANNEL}',
                    json_build_object('schema', TG_TABLE_SCHEMA, 'table', TG_TABLE_NAME, 'op', TG_OP)::text
                );
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        ''')
        cur.execute(f'DROP TRIGGER IF EXISTS contracts_notify_trigger ON "{schema_name}"."contracts"')
        cur.execute(f'''
            CREATE TRIGGER contracts_notify_trigger
            AFTER INSERT OR UPDATE ON "{schema_name}"."contracts"
            FOR EACH STATEMENT EXECUTE FUNCTION "{schema_name}".notify_contracts_loaded()
        ''')
    conn.commit()


def push_contracts(conn: psycopg.connection.Connection, schema_name: str, date_dir: str) -> None:
    """Push all contracts/symbols into a single "contracts" table (all exchanges)."""
    contract_rows = export.aggregate_contract_rows(date_dir)
    if not contract_rows:
        print(f"    No contracts to push for {date_dir}")
        return

    _drop_create_table(conn, schema_name, "contracts", CONTRACT_COLUMN_DDL)
    if schema_name == paths.POSTGRES_STATIC_SCHEMA:
        # Attach trigger before COPY so this push's own load fires the notify too --
        # not just the next one.
        _ensure_contracts_notify_trigger(conn, schema_name)
    n = _copy_rows(conn, schema_name, "contracts", paths.CONTRACT_COLUMNS, contract_rows, NULLABLE_CONTRACT_COLUMNS)
    if schema_name == paths.POSTGRES_STATIC_SCHEMA:
        print(f"    Pushed {n} rows -> {schema_name}.contracts (notify '{CONTRACTS_NOTIFY_CHANNEL}' fired)")
    else:
        print(f"    Pushed {n} rows -> {schema_name}.contracts")


def push_baskets_nexus(conn: psycopg.connection.Connection, basket_schema: str, date_dir: str) -> None:
    """Push one table per basket name into basket_schema -- the shape Nexus's own basket loader expects."""
    basket_rows = export.aggregate_basket_rows(date_dir)
    if not basket_rows:
        print(f"    No baskets to push for {date_dir}")
        return

    by_basket: Dict[str, List[dict]] = {}
    for row in basket_rows:
        by_basket.setdefault(row.get("basket", ""), []).append(row)

    total = 0
    for basket_name, rows in by_basket.items():
        _drop_create_table(conn, basket_schema, basket_name, NEXUS_BASKET_COLUMN_DDL)
        n = _copy_rows(conn, basket_schema, basket_name, paths.NEXUS_BASKET_COLUMNS, rows, NULLABLE_BASKET_COLUMNS)
        total += n
        print(f"    loaded {n} rows -> {basket_schema}.{basket_name}")

    print(f"      Pushed {total} rows across {len(by_basket)} basket tables")


def push_baskets_merged(conn: psycopg.connection.Connection, schema_name: str, date_dir: str) -> None:
    """Push all baskets into a single "baskets" table (convenience mirror; Nexus doesn't read this)."""
    basket_rows = export.aggregate_basket_rows(date_dir)
    if not basket_rows:
        print(f"    No baskets to push for {date_dir}")
        return

    columns = ["basket"] + paths.NEXUS_BASKET_COLUMNS
    _drop_create_table(conn, schema_name, "baskets", MERGED_BASKET_COLUMN_DDL)
    n = _copy_rows(conn, schema_name, "baskets", columns, basket_rows, NULLABLE_BASKET_COLUMNS)
    print(f"    Pushed {n} rows -> {schema_name}.baskets")


def run(opts: runner.Opts) -> None:
    """Push normalized data to Postgres: dated schemas (Nexus-compatible) + always-current public mirror."""
    if opts.dry_run:
        print("DRY RUN: Would push to Postgres")
        return

    try:
        db_url = config.database_url(opts.database_url)
    except ValueError as e:
        print(f"  Error: {e}")
        return

    dated_schema = paths.postgres_schema(opts.date_dir)
    dated_basket_schema = paths.postgres_baskets_schema(opts.date_dir)
    static_schema = paths.POSTGRES_STATIC_SCHEMA

    try:
        with psycopg.connect(db_url) as conn:
            create_schema(conn, dated_schema)
            create_schema(conn, dated_basket_schema)

            print(f"  Pushing to {dated_schema} / {dated_basket_schema}...")
            push_contracts(conn, dated_schema, opts.date_dir)
            push_baskets_merged(conn, dated_schema, opts.date_dir)
            push_baskets_nexus(conn, dated_basket_schema, opts.date_dir)

            print(f"  Pushing to {static_schema} (always-current mirror)...")
            push_contracts(conn, static_schema, opts.date_dir)
            push_baskets_merged(conn, static_schema, opts.date_dir)

        print(f"  Successfully pushed to {dated_schema}, {dated_basket_schema}, {static_schema}")
    except Exception as e:
        print(f"  Error pushing to Postgres: {e}")
        raise
