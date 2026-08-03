"""SQLite export: push normalized data to SQLite database."""
import sqlite3
from pathlib import Path
from typing import List

import pandas as pd

from . import export, paths, runner


def create_tables(conn: sqlite3.Connection) -> None:
    """Create contracts and baskets tables."""
    cur = conn.cursor()

    # Contracts table (all TEXT columns)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contracts (
        date TEXT,
        exchange TEXT,
        scriptDetails TEXT,
        scriptInstrumentType TEXT,
        scriptInstrumentType2 TEXT,
        multiplier TEXT,
        lotSize TEXT,
        tickSize TEXT,
        ISIN TEXT,
        tradingSessionUTC TEXT,
        expiration TEXT,
        script TEXT,
        scriptToken TEXT,
        underlying_root TEXT,
        underlying TEXT,
        strike TEXT,
        optionType TEXT,
        currency TEXT,
        brokerScript1 TEXT,
        brokerScript2 TEXT,
        brokerScript3 TEXT,
        brokerScript4 TEXT
    )
    """)

    # Baskets table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS baskets (
        date TEXT,
        basket TEXT,
        symbol TEXT
    )
    """)

    conn.commit()


def push_contracts(conn: sqlite3.Connection, date_dir: str) -> None:
    """Push contracts to SQLite."""
    contract_rows = export.aggregate_contract_rows(date_dir)
    if not contract_rows:
        print(f"    No contracts to push for {date_dir}")
        return

    df = pd.DataFrame(contract_rows)
    cols = ["date", "exchange"] + paths.NORMALIZED_COLUMNS

    # Ensure all columns exist
    for col in cols:
        if col not in df.columns:
            df[col] = ""

    df = df[cols]

    print(f"    Pushing {len(df)} contracts...")

    with conn:
        for _, row in df.iterrows():
            values = [row[col] for col in cols]
            placeholders = ", ".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO contracts ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )

    print(f"      Pushed {len(df)} rows")


def push_baskets(conn: sqlite3.Connection, date_dir: str) -> None:
    """Push baskets to SQLite."""
    basket_rows = export.aggregate_basket_rows(date_dir)
    if not basket_rows:
        print(f"    No baskets to push for {date_dir}")
        return

    df = pd.DataFrame(basket_rows)
    print(f"    Pushing {len(df)} baskets...")

    with conn:
        for _, row in df.iterrows():
            conn.execute(
                "INSERT INTO baskets (date, basket, symbol) VALUES (?, ?, ?)",
                (row["date"], row["basket"], row["symbol"]),
            )

    print(f"      Pushed {len(df)} rows")


def run(opts: runner.Opts) -> None:
    """Push normalized data to SQLite test database."""
    if opts.dry_run:
        print("DRY RUN: Would push to SQLite")
        return

    if not hasattr(opts, "test_db_file") or not opts.test_db_file:
        print("    No --test-db file specified, skipping SQLite export")
        return

    db_file = Path(opts.test_db_file)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Pushing to SQLite: {db_file}...")

    try:
        conn = sqlite3.connect(db_file)
        create_tables(conn)
        push_contracts(conn, opts.date_dir)
        push_baskets(conn, opts.date_dir)
        conn.close()

        print(f"  Successfully pushed to {db_file}")
    except Exception as e:
        print(f"  Error pushing to SQLite: {e}")
        raise
