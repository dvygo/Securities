"""ClickHouse export: push normalized contracts and baskets, staged then swapped.

Replaces the old Postgres contracts push. postgres_export_plugin.py is untouched
and still writes to Postgres -- only this side moved.

Layout, per push, inside the configured database:
  contracts_YYYYMMDD   dated snapshot of the day's contracts (all exchanges)
  contracts            always-current mirror, overwritten every run
  baskets_YYYYMMDD     dated snapshot: one row per basket, scripts as Array(String)
  baskets              always-current mirror

Every target is loaded through a staging table and then swapped in with
EXCHANGE TABLES, which is atomic: a reader querying `contracts` sees either the
whole previous load or the whole new one, never a partially-inserted table. The
old Postgres path was DROP+CREATE+COPY, so the table did not exist at all for the
length of the load -- survivable for a batch job, not for the always-current
mirror something else is querying.

Rows are inserted in EXPORT_BATCH_ROWS batches straight off export's iterator,
so an --all-symbols GLBX day (~1.09M contracts) is never held whole.

EXCHANGE TABLES requires the database to use the Atomic engine, which is the
default from ClickHouse 20.10 onward. On an Ordinary database it fails, and
_swap_into_place says so rather than leaving the load in the staging table.
"""
from typing import Dict, Iterable, List, Optional, Sequence

import clickhouse_connect

from . import config, export, paths, runner

# Rows per INSERT. Matches export.CONTRACT_BATCH_ROWS -- the iterator already
# yields in that unit, so this is here to name the intent, not to re-chunk.
EXPORT_BATCH_ROWS = export.CONTRACT_BATCH_ROWS

# Canonical columns holding an integer. Everything else in CONTRACT_COLUMNS is
# String. These are Nullable because they legitimately go blank -- tickSize is
# always empty (it belongs to the interactive layer), counterToken/counterTokenV2
# are empty for any venue with no venue_id configured, and an equity has no
# strike or expiration. Blank
# cells become NULL at insert (see _cell), never 0, so "no strike" and "strike 0"
# stay distinguishable.
CONTRACT_INT_COLUMNS = {
    "multiplier", "lotSize", "tickSize", "expiration",
    "scriptToken", "strike", "counterToken", "counterTokenV2",
}

# The definition passthrough stays String, including the numeric fields.
#
# Those columns are a verbatim copy of what Databento sent, at Databento's own
# 1e-9 fixed-point scale, and the CSV they come from is untyped text. Typing them
# here would mean this module holding a second opinion about 70 vendor fields --
# and getting one wrong turns a value into NULL silently. A query casts what it
# needs: toInt64(def_min_price_increment).
#
# The canonical columns above are different: their types are this pipeline's own,
# asserted in the normalizer, and worth having in a database meant for scans.


def contracts_table(date_dir: str) -> str:
    """Dated contracts table name: contracts_YYYYMMDD."""
    return f"contracts_{_validated_date(date_dir)}"


def baskets_table(date_dir: str) -> str:
    """Dated baskets table name: baskets_YYYYMMDD."""
    return f"baskets_{_validated_date(date_dir)}"


def _validated_date(date_dir: str) -> str:
    """date_dir, checked as YYYYMMDD.

    ClickHouse table names are this module's own, so they are built from the
    date directly rather than from paths.dated_schema() -- deriving one naming
    scheme by string-stripping another's prefix only couples them. The
    validation is still shared, via the function that owns the format.
    """
    paths.dated_schema(date_dir)  # raises on a malformed date_dir
    return date_dir


# Always-current mirrors, overwritten every run.
CURRENT_CONTRACTS_TABLE = "contracts"
CURRENT_BASKETS_TABLE = "baskets"


def _contract_column_ddl() -> List[tuple]:
    """(name, type) for every contracts column, in CONTRACT_COLUMNS order."""
    return [
        (col, "Nullable(Int64)" if col in CONTRACT_INT_COLUMNS else "String")
        for col in paths.CONTRACT_COLUMNS
    ]


BASKET_COLUMNS = ["date", "basket", "scripts"]
BASKET_COLUMN_DDL = [
    ("date", "String"),
    ("basket", "String"),
    # Array(String), not a JSON blob: ClickHouse has no jsonb, and the Postgres
    # side only used one because Nexus reads that column. Nothing reads this one
    # yet, so it gets the type that actually queries -- hasAny/arrayJoin work.
    ("scripts", "Array(String)"),
]


def _cell(row: dict, column: str):
    """One value for INSERT, blank-to-NULL for the integer columns.

    Values arrive as CSV text. ClickHouse rejects "" for Int64, so the integer
    columns have to hand over None; String columns keep "" as-is, since an empty
    string is a real value there and turning it into NULL would lose the
    distinction for anything reading back.
    """
    value = row.get(column, "")
    if column in CONTRACT_INT_COLUMNS:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            # A non-numeric value in a numeric column is a normalizer bug, not
            # something to paper over with 0 -- NULL keeps it visible in a scan.
            return None
    return "" if value is None else str(value)


def _staging_name(table: str) -> str:
    """Staging table paired with a target.

    Not PID-scoped, unlike the CSV staging paths elsewhere in the pipeline: two
    concurrent pushes of the same day would corrupt each other at the swap no
    matter how the staging table is named, so a fixed name at least makes the
    collision visible instead of leaving orphaned per-PID tables behind.
    """
    return f"{table}_staging"


def _drop_create(client, database: str, table: str, column_ddl: Sequence[tuple], order_by: str) -> None:
    cols_sql = ",\n    ".join(f'"{name}" {ddl}' for name, ddl in column_ddl)
    client.command(f'DROP TABLE IF EXISTS "{database}"."{table}"')
    client.command(
        f'CREATE TABLE "{database}"."{table}" (\n    {cols_sql}\n) '
        f"ENGINE = MergeTree ORDER BY ({order_by})"
    )


def _ensure_target(client, database: str, table: str, column_ddl: Sequence[tuple], order_by: str) -> None:
    """Create the target if it is not there yet.

    EXCHANGE TABLES needs both sides to exist, so a first-ever push would fail
    without this. IF NOT EXISTS rather than DROP: the whole point of the swap is
    that the target keeps serving its previous contents until the new load is
    complete.
    """
    cols_sql = ",\n    ".join(f'"{name}" {ddl}' for name, ddl in column_ddl)
    client.command(
        f'CREATE TABLE IF NOT EXISTS "{database}"."{table}" (\n    {cols_sql}\n) '
        f"ENGINE = MergeTree ORDER BY ({order_by})"
    )


def _swap_into_place(client, database: str, staging: str, target: str) -> None:
    """Atomically swap staging into target, then drop what was there before."""
    try:
        client.command(
            f'EXCHANGE TABLES "{database}"."{staging}" AND "{database}"."{target}"'
        )
    except Exception as e:
        raise RuntimeError(
            f"EXCHANGE TABLES failed for {database}.{target}: {e}. The new rows are "
            f"in {database}.{staging} and {database}.{target} still holds the previous "
            f"load. EXCHANGE requires an Atomic database engine (the default since "
            f"ClickHouse 20.10); check with "
            f"SELECT engine FROM system.databases WHERE name = '{database}'."
        ) from e
    # staging now holds the previous load.
    client.command(f'DROP TABLE IF EXISTS "{database}"."{staging}"')


def _load_batches(
    client,
    database: str,
    table: str,
    columns: Sequence[str],
    batches: Iterable[List[dict]],
    label: str,
) -> int:
    """Insert pre-batched rows, reporting a running total."""
    total = 0
    for batch in batches:
        if not batch:
            continue
        client.insert(
            table=table,
            database=database,
            column_names=list(columns),
            data=[[_cell(row, col) for col in columns] for row in batch],
        )
        total += len(batch)
        print(f"      {total} row(s) -> {label}...", flush=True)
    return total


def push_contracts(client, database: str, date_dir: str) -> int:
    """Load the day's contracts into the dated table and the current mirror.

    The rows are read from disk once and inserted into the dated staging table,
    then the mirror is filled from that table server-side rather than by
    re-reading ~1.09M rows over HTTP a second time.
    """
    dated = contracts_table(date_dir)
    dated_staging = _staging_name(dated)
    column_ddl = _contract_column_ddl()
    order_by = '"exchange", "script"'

    _ensure_target(client, database, dated, column_ddl, order_by)
    _drop_create(client, database, dated_staging, column_ddl, order_by)

    total = _load_batches(
        client, database, dated_staging, paths.CONTRACT_COLUMNS,
        export.iter_contract_rows(date_dir, EXPORT_BATCH_ROWS), f"{database}.{dated}",
    )
    if not total:
        print(f"    No contracts to push for {date_dir}")
        client.command(f'DROP TABLE IF EXISTS "{database}"."{dated_staging}"')
        return 0

    _swap_into_place(client, database, dated_staging, dated)
    print(f"    Pushed {total} rows -> {database}.{dated}")

    # Current mirror, filled from the dated table that was just swapped in.
    current_staging = _staging_name(CURRENT_CONTRACTS_TABLE)
    _ensure_target(client, database, CURRENT_CONTRACTS_TABLE, column_ddl, order_by)
    _drop_create(client, database, current_staging, column_ddl, order_by)
    client.command(
        f'INSERT INTO "{database}"."{current_staging}" SELECT * FROM "{database}"."{dated}"'
    )
    _swap_into_place(client, database, current_staging, CURRENT_CONTRACTS_TABLE)
    print(f"    Pushed {total} rows -> {database}.{CURRENT_CONTRACTS_TABLE} (current mirror)")
    return total


def push_baskets(client, database: str, date_dir: str) -> int:
    """Load the day's baskets into the dated table and the current mirror."""
    grouped: Dict[str, List[str]] = {}
    for row in export.aggregate_basket_rows(date_dir):
        basket, script = row.get("basket", ""), row.get("script", "")
        if not basket or not script:
            continue
        grouped.setdefault(basket, []).append(script)

    if not grouped:
        print(f"    No baskets to push for {date_dir}")
        return 0

    rows = [{"date": date_dir, "basket": name, "scripts": scripts} for name, scripts in grouped.items()]
    order_by = '"basket"'
    dated = baskets_table(date_dir)

    for target in (dated, CURRENT_BASKETS_TABLE):
        staging = _staging_name(target)
        _ensure_target(client, database, target, BASKET_COLUMN_DDL, order_by)
        _drop_create(client, database, staging, BASKET_COLUMN_DDL, order_by)
        client.insert(
            table=staging,
            database=database,
            column_names=BASKET_COLUMNS,
            data=[[r["date"], r["basket"], r["scripts"]] for r in rows],
        )
        _swap_into_place(client, database, staging, target)
        print(f"    Pushed {len(rows)} baskets -> {database}.{target}")

    return len(rows)


def connect(cfg: Optional[config.ClickHouseCfg] = None):
    """Open a client against the configured server, creating the database if needed."""
    cfg = cfg or config.load_clickhouse()
    client = clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        secure=cfg.secure,
    )
    client.command(f'CREATE DATABASE IF NOT EXISTS "{cfg.database}"')
    return client


def run(opts: runner.Opts) -> None:
    """Push normalized data to ClickHouse: dated tables + always-current mirrors."""
    if opts.dry_run:
        print("DRY RUN: Would push to ClickHouse")
        return

    cfg = config.load_clickhouse()
    print(f"  Pushing to ClickHouse {cfg.host}:{cfg.port}/{cfg.database}...")

    try:
        client = connect(cfg)
    except Exception as e:
        print(f"  Error: cannot reach ClickHouse at {cfg.host}:{cfg.port}: {e}")
        return

    try:
        push_contracts(client, cfg.database, opts.date_dir)
        push_baskets(client, cfg.database, opts.date_dir)
    finally:
        client.close()

    print(f"  Successfully pushed to {cfg.database}")
