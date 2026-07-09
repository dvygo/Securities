"""Postgres export: push normalized data via psycopg with COPY."""
from pathlib import Path
from typing import List

import pandas as pd
import psycopg
from psycopg import sql

from . import config, export, paths, runner


def create_schema(conn: psycopg.connection.Connection, schema_name: str) -> None:
    """Create schema if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name)))
    conn.commit()


def create_tables(conn: psycopg.connection.Connection, schema_name: str) -> None:
    """Create symbol and basket tables in schema."""
    # Symbol table (from normalized columns)
    symbol_table_ddl = f"""
    CREATE TABLE IF NOT EXISTS {schema_name}.symbols (
        date TEXT,
        exchange TEXT,
        scriptDetails TEXT,
        scriptInstrumentType TEXT,
        scriptInstrumentType2 TEXT,
        multiplier BIGINT,
        lotSize BIGINT,
        tickSize BIGINT,
        ISIN TEXT,
        tradingSessionUTC TEXT,
        expiration BIGINT,
        script TEXT,
        scriptToken TEXT,
        underlying_root TEXT,
        underlying TEXT,
        strike BIGINT,
        optionType TEXT,
        currency TEXT,
        CONSTRAINT symbols_pk UNIQUE(scriptToken, script)
    );
    CREATE INDEX IF NOT EXISTS symbols_underlying_idx ON {schema_name}.symbols (underlying_root);
    CREATE INDEX IF NOT EXISTS symbols_strike_idx ON {schema_name}.symbols (strike);
    CREATE INDEX IF NOT EXISTS symbols_type_idx ON {schema_name}.symbols (scriptInstrumentType);
    """

    # Baskets table
    baskets_table_ddl = f"""
    CREATE TABLE IF NOT EXISTS {schema_name}.baskets (
        date TEXT,
        basket TEXT,
        symbol TEXT
    );
    """

    with conn.cursor() as cur:
        cur.execute(symbol_table_ddl)
        cur.execute(baskets_table_ddl)
    conn.commit()


def push_contracts(
    conn: psycopg.connection.Connection,
    schema_name: str,
    date_dir: str,
) -> None:
    """Push contracts/symbols to Postgres via COPY."""
    contract_rows = export.aggregate_contract_rows(date_dir)
    if not contract_rows:
        print(f"    No contracts to push for {date_dir}")
        return

    df = pd.DataFrame(contract_rows)

    # Prepare data for COPY (ensure correct column order and types)
    copy_cols = ["date", "exchange"] + paths.NORMALIZED_COLUMNS
    df = df[copy_cols]

    # Convert numeric columns to int/float as needed
    for col in ["multiplier", "lotSize", "tickSize", "expiration", "strike", "scriptToken"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # Replace NaN with NULL for COPY
    df = df.fillna("")

    # Push via COPY
    print(f"    Pushing {len(df)} symbols...")
    with conn.cursor() as cur:
        with cur.copy(f"COPY {schema_name}.symbols ({','.join(copy_cols)}) FROM STDIN") as copy:
            for _, row in df.iterrows():
                line = "|".join(str(v) for v in row.values) + "\n"
                copy.write(line.encode())

    conn.commit()
    print(f"      Pushed {len(df)} rows")


def push_baskets(
    conn: psycopg.connection.Connection,
    schema_name: str,
    date_dir: str,
) -> None:
    """Push baskets to Postgres."""
    basket_rows = export.aggregate_basket_rows(date_dir)
    if not basket_rows:
        print(f"    No baskets to push for {date_dir}")
        return

    df = pd.DataFrame(basket_rows)
    print(f"    Pushing {len(df)} basket entries...")

    with conn.cursor() as cur:
        with cur.copy("COPY " + schema_name + ".baskets (date, basket, symbol) FROM STDIN") as copy:
            for _, row in df.iterrows():
                line = f"{row['date']}|{row['basket']}|{row['symbol']}\n"
                copy.write(line.encode())

    conn.commit()
    print(f"      Pushed {len(df)} rows")


def run(opts: runner.Opts) -> None:
    """Push normalized data to Postgres."""
    if opts.dry_run:
        print("DRY RUN: Would push to Postgres")
        return

    # Get database URL
    try:
        db_url = config.database_url(opts.database_url)
    except ValueError as e:
        print(f"  Error: {e}")
        return

    schema_name = paths.postgres_schema(opts.date_dir)

    print(f"  Pushing to Postgres schema {schema_name}...")

    try:
        with psycopg.connect(db_url) as conn:
            create_schema(conn, schema_name)
            create_tables(conn, schema_name)
            push_contracts(conn, schema_name, opts.date_dir)
            push_baskets(conn, schema_name, opts.date_dir)

        print(f"  Successfully pushed to {schema_name}")
    except Exception as e:
        print(f"  Error pushing to Postgres: {e}")
        raise
