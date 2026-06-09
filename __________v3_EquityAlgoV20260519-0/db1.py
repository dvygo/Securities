import configparser
import os
import json
import asyncio
import asyncpg
import pandas as pd
from pathlib import Path
from urllib.parse import unquote, urlparse
import re
import logging
from typing import Optional

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Paths / DB config (EquityAlgo postgres — see secrets/secrets.ini [postgres_equityalgo])
# ─────────────────────────────────────────────────────────────
CONFIG_INI = Path(__file__).resolve().parent.parent / "secrets" / "secrets.ini"
BASE_DIR = Path(__file__).resolve().parent / "output_matrix"
UNDERLYINGS_FILE = Path(__file__).resolve().parent / "underlying.txt"


def get_database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return url
    if not CONFIG_INI.is_file():
        raise SystemExit(f"Missing {CONFIG_INI}")
    cp = configparser.ConfigParser()
    cp.read(CONFIG_INI, encoding="utf-8")
    u = cp.get("postgres_equityalgo", "database_url", fallback="").strip()
    if not u:
        raise SystemExit(
            f"Set DATABASE_URL or [postgres_equityalgo] database_url in {CONFIG_INI}"
        )
    return u


def asyncpg_kwargs_from_url(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise SystemExit(f"Unsupported database URL scheme: {u.scheme!r}")
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": (u.path or "").lstrip("/") or "postgres",
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
    }

# ─────────────────────────────────────────────────────────────
# Time shift configuration
# ─────────────────────────────────────────────────────────────
SHIFT_AFTER_DATE = "2026-03-09"
SHIFT_MINUTES = 60
SHIFT_AFTER_DATE_DT = pd.to_datetime(SHIFT_AFTER_DATE)

# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────
async def ensure_timescale(conn):
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
        logger.info("TimescaleDB extension ensured")
        return True
    except Exception as e:
        logger.warning(f"TimescaleDB not available: {e}")
        return False


def safe_ident(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return "unknown"
    if name[0].isdigit():
        name = "x_" + name
    return name


def schema_for_underlying(underlying: str) -> str:
    return safe_ident(f"XNAS_{underlying.strip().upper()}")


def parse_underlying_and_dte_from_dir(dirname: str):
    m = re.match(r"^(.+?)_(\d+)dte$", dirname.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"Bad dir format: {dirname}")
    underlying = m.group(1).upper()
    return schema_for_underlying(underlying), int(m.group(2)), underlying


async def create_schema_and_table(conn, schema, dte, use_timescale):

    table_name = f"dte_{dte}"

    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}";')

    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{schema}"."{table_name}" (
            time_ts TIMESTAMPTZ NOT NULL,
            future_data JSONB DEFAULT '{{}}'::jsonb,
            straddle_data JSONB DEFAULT '{{}}'::jsonb,
            vt_data JSONB DEFAULT '{{}}'::jsonb,
            PRIMARY KEY (time_ts)
        );
    """)

    if use_timescale:
        await conn.execute(f"""
            SELECT create_hypertable(
                '"{schema}"."{table_name}"',
                'time_ts',
                if_not_exists => TRUE
            );
        """)

    return table_name


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def extract_trade_date_from_filename(path: Path) -> str:
    """Trade date as YYYY-MM-DD from matrix CSV name (hyphen or compact)."""
    name = path.name
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"matrix_(\d{8})", name, re.IGNORECASE)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError(f"no trade date in filename: {name}")


def safe_float(val) -> Optional[float]:
    if pd.isna(val):
        return None
    return float(val)


def load_allowed_underlyings(txt_path: Path):
    allowed = set()
    for line in txt_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            allowed.add(line.upper())
    return allowed


def extract_symbol_from_csv_name(csv_path: Path):
    m = re.match(r"^([A-Za-z0-9]+)_matrix_", csv_path.stem)
    if not m:
        raise ValueError(csv_path.name)
    return m.group(1).upper()


# ─────────────────────────────────────────────────────────────
# CSV processing
# ─────────────────────────────────────────────────────────────
async def process_csv(pool, csv_path, schema, table_name):

    logger.info(f"Processing {csv_path.name}")

    df = pd.read_csv(csv_path)

    if "timestamp" in df.columns:
        time_col = "timestamp"
    elif "ts" in df.columns:
        time_col = "ts"
    else:
        logger.error("Missing timestamp column")
        return

    required = ["underlying_ltp", "straddle_3atm", "vt_3atm"]
    for col in required:
        if col not in df.columns:
            logger.error(f"{csv_path.name} missing {col}")
            return

    trade_date = extract_trade_date_from_filename(csv_path)
    trade_date_dt = pd.to_datetime(trade_date)

    ts = pd.to_datetime(df[time_col], utc=True, format="mixed")

    base_time = (
        pd.Timestamp("1970-01-01", tz="UTC")
        + (ts - ts.dt.normalize())
    )

    # apply shift after specific date
    if trade_date_dt >= SHIFT_AFTER_DATE_DT:
        logger.info(f"Applying {SHIFT_MINUTES} min shift for {csv_path.name}")
        base_time = base_time + pd.Timedelta(minutes=SHIFT_MINUTES)

    df["time_ts"] = base_time

    records = []

    for time_val, grp in df.groupby("time_ts"):
        records.append((
            time_val,
            json.dumps({trade_date: safe_float(grp["underlying_ltp"].iloc[0])}),
            json.dumps({trade_date: safe_float(grp["straddle_3atm"].iloc[0])}),
            json.dumps({trade_date: safe_float(grp["vt_3atm"].iloc[0])}),
        ))

    async with pool.acquire() as conn:

        await conn.execute("SET TIME ZONE 'UTC';")

        async with conn.transaction():
            await conn.executemany(
                f"""
                INSERT INTO "{schema}"."{table_name}"
                    (time_ts, future_data, straddle_data, vt_data)
                VALUES ($1,$2::jsonb,$3::jsonb,$4::jsonb)
                ON CONFLICT (time_ts)
                DO UPDATE SET
                    future_data =
                        COALESCE("{schema}"."{table_name}".future_data,'{{}}')
                        || EXCLUDED.future_data,

                    straddle_data =
                        COALESCE("{schema}"."{table_name}".straddle_data,'{{}}')
                        || EXCLUDED.straddle_data,

                    vt_data =
                        COALESCE("{schema}"."{table_name}".vt_data,'{{}}')
                        || EXCLUDED.vt_data;
                """,
                records,
            )

    logger.info(f"Inserted {len(records)} rows")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
async def main():

    logger.info("Starting ingestion")

    db_url = get_database_url()
    logger.info("Database %s", db_url.rsplit("@", 1)[-1])

    allowed_underlyings = load_allowed_underlyings(UNDERLYINGS_FILE)
    if not allowed_underlyings:
        raise SystemExit(f"No symbols in {UNDERLYINGS_FILE}")

    pool = await asyncpg.create_pool(
        **asyncpg_kwargs_from_url(db_url),
        min_size=2,
        max_size=6,
    )

    try:

        async with pool.acquire() as conn:
            use_timescale = await ensure_timescale(conn)

        for dte_dir in sorted(BASE_DIR.iterdir()):

            if not dte_dir.is_dir():
                continue

            try:
                schema, dte, underlying = parse_underlying_and_dte_from_dir(
                    dte_dir.name
                )
            except Exception as e:
                logger.warning(e)
                continue

            if underlying not in allowed_underlyings:
                continue

            async with pool.acquire() as conn:
                table_name = await create_schema_and_table(
                    conn, schema, dte, use_timescale
                )
            logger.info("Schema %s.%s", schema, table_name)

            csv_files = sorted(dte_dir.glob("*.csv"))

            sem = asyncio.Semaphore(5)

            async def guarded(f):
                async with sem:
                    sym = extract_symbol_from_csv_name(f)
                    if sym in allowed_underlyings:
                        await process_csv(pool, f, schema, table_name)

            await asyncio.gather(*(guarded(f) for f in csv_files))

    except KeyboardInterrupt:
        logger.info("Interrupted (Ctrl+C). Closing pool.")
        raise
    finally:
        await pool.close()
        logger.info("Ingestion completed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None

