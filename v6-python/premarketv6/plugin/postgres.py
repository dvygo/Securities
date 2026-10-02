"""Postgres plugin-table appender: push data/YYYYMMDD/TRANSFORM/plugin/*.parquet (legacy
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

Rows matching NOT_PUSHED stay in the plugin files and are left out of the push
only.

Price columns are pushed in the market-data feed's units (FEED_PRICE_SCALE), not
the plugin file's. That too is a push-side change: normalize, the plugin file,
the MDF token map and the AlphaEMS master keep their own scale.

Cash equities are pushed with fullname "SPOT <symbol>" (SPOT_FULLNAME), beside
the "FUT ..." and "OPT ..." the derivatives already carry. Push-side only, too.
"""
import csv
import io
from typing import List

import psycopg

from .. import config, parquet_export
from . import build

# Column types for the plugin table, from docs/plugin/pg_data_types.txt.
# Keyed by column name and rendered in build.PLUGIN_COLUMNS order, so a
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

# (exch, insttype) rows that are built but never pushed. OPRA's underlying
# spots (instrument_class K, see normalize/databento_norm.py) are only the
# reference leg its options point at, and each carries the same fullname as the
# XNAS EQUITY listing -- pushed, every optionable stock showed up twice under
# two tokens (6,288 rows on 2026-09-28, 5,933 of them doubling an XNAS name).
# normalize, the plugin file and the MDF token map keep them.
NOT_PUSHED = {("XCBO", "SPOT")}

# Price units on hft.marketdata.fo, per venue: what the MDF lane's
# TOB_price_divisor leaves of Databento's 10^9 prices (cpp-vendor.ini: 10^7 for
# XCME and XNAS, 10^5 for XCBO). A consumer divides a feed price by the row's
# divisor, so divisor, strikeprice and ticksize are pushed in these units:
# ESZ6 goes from divisor 1000000000 / ticksize 250000000 to 100 / 25. Change a
# lane's divisor and this table has to follow.
FEED_PRICE_SCALE = {"XCME": 100, "XNAS": 100, "XCBO": 10000}

# (exch, insttype) rows whose fullname is pushed as "SPOT <symbol>". A cash
# equity's fullname is otherwise the bare ticker ("AAPL"), while every future
# and option reads "FUT ..." / "OPT ...": the operator UI's search and ranking
# work on that leading word, so a bare ticker was not found as a spot and sat
# among its own options. symbol and name stay the bare ticker -- they are what
# a consumer trades and joins on.
SPOT_FULLNAME = {("XNAS", "EQUITY")}
_SPOT_PREFIX = "SPOT "

# Staging table for the upsert. TEMP, so it is per-connection and cannot
# collide with a concurrent push on another connection.
_TEMP_TABLE = "plugin_upsert_staging"


def _create_table_sql(schema: str, table: str) -> str:
    """DDL for the plugin table, in PLUGIN_COLUMNS order."""
    missing = [c for c in build.PLUGIN_COLUMNS if c not in PLUGIN_COLUMN_TYPES]
    if missing:
        raise ValueError(
            f"No Postgres type for plugin column(s): {', '.join(missing)}. "
            f"Add them to PLUGIN_COLUMN_TYPES (see docs/plugin/pg_data_types.txt)."
        )
    cols = ",\n".join(
        f'    "{c}" {PLUGIN_COLUMN_TYPES[c]}' for c in build.PLUGIN_COLUMNS
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


def _whole(value) -> int:
    """A plugin numeric column as an int; blank or unparseable is 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_feed_scale(row: dict) -> tuple:
    """Returns (row, inexact) with divisor, strikeprice and ticksize in feed units.

    Rows of a venue not in FEED_PRICE_SCALE, or already at or below the feed's
    scale, come back untouched. strikeprice <= 0 (the no-strike marker) and
    ticksize <= 1 (the "tick depends on the price" marker) are not prices and are
    left alone. `inexact` is True when the strike is not a whole number of feed
    units and had to be rounded.
    """
    scale = FEED_PRICE_SCALE.get(row.get("exch"))
    divisor = _whole(row.get("divisor"))
    if not scale or divisor <= scale or divisor % scale:
        return row, False
    factor = divisor // scale
    out = dict(row)
    out["divisor"] = str(scale)
    inexact = False
    strike = _whole(row.get("strikeprice"))
    if strike > 0:
        inexact = strike % factor != 0
        out["strikeprice"] = str((strike + factor // 2) // factor)
    tick = _whole(row.get("ticksize"))
    if tick > 1:
        out["ticksize"] = str(tick // factor) if tick % factor == 0 else repr(tick / factor)
    return out, inexact


def _spot_fullname(row: dict) -> dict:
    """Returns the row with fullname "SPOT <symbol>" if it is a SPOT_FULLNAME row.

    Any other row, and one already carrying the prefix, comes back as the same
    object. The ticker is the row's symbol, falling back to its fullname.
    """
    if (row.get("exch"), row.get("insttype")) not in SPOT_FULLNAME:
        return row
    ticker = str(row.get("symbol") or row.get("fullname") or "").strip()
    if not ticker or str(row.get("fullname") or "").startswith(_SPOT_PREFIX):
        return row
    out = dict(row)
    out["fullname"] = _SPOT_PREFIX + ticker
    return out


class _Pushable:
    """Iterates batches of plugin rows as they are pushed.

    NOT_PUSHED rows are left out and counted in `dropped`. The rest are converted
    to the feed's price units; `rescaled` counts those rows and `inexact` the
    strikes that needed rounding. SPOT_FULLNAME rows get their "SPOT " fullname,
    counted in `spot_named`.
    """

    def __init__(self, batches):
        self.batches = batches
        self.dropped = 0
        self.rescaled = 0
        self.inexact = 0
        self.spot_named = 0

    def __iter__(self):
        for rows in self.batches:
            kept = []
            for r in rows:
                if (r.get("exch"), r.get("insttype")) in NOT_PUSHED:
                    self.dropped += 1
                    continue
                out, inexact = _to_feed_scale(r)
                self.rescaled += out is not r
                self.inexact += inexact
                named = _spot_fullname(out)
                self.spot_named += named is not out
                kept.append(named)
            yield kept


def _upsert_sql(schema: str, table: str, columns: List[str]) -> str:
    """INSERT ... ON CONFLICT DO UPDATE moving the staging rows into the target.

    Every non-key column is overwritten from EXCLUDED, so our values win over
    whatever is already stored. The key columns are excluded from the SET list
    because they are what matched -- assigning them would be a no-op Postgres
    rejects.
    """
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    pk = ", ".join(f'"{c}"' for c in PLUGIN_PRIMARY_KEY)
    updates = ", ".join(
        f'"{c}" = EXCLUDED."{c}"' for c in columns if c not in PLUGIN_PRIMARY_KEY
    )
    if not updates:
        raise ValueError("Every plugin column is part of the primary key; nothing to update.")
    return (
        f'INSERT INTO "{schema}"."{table}" ({quoted_cols}) '
        f'SELECT DISTINCT ON ({pk}) {quoted_cols} FROM "{_TEMP_TABLE}" '
        f"ON CONFLICT ({pk}) DO UPDATE SET {updates}"
    )


def _copy_upsert(conn: psycopg.connection.Connection, schema: str, table: str,
                 columns: List[str], batches) -> tuple:
    """Upsert batches of rows into schema.table. Returns (rows_read, rows_affected).

    Our values always win: a (token, trade_date) already in the table is
    overwritten, not appended beside and not skipped. Re-pushing a day is
    therefore idempotent -- the previous push's row for a contract is replaced
    by this one's rather than raising a duplicate-key error partway through and
    leaving the day half written.

    COPY cannot do ON CONFLICT, so the rows land in a TEMP table first and move
    across in one INSERT ... ON CONFLICT DO UPDATE. The COPY into the temp table
    still streams batch by batch, so the memory profile is unchanged; the temp
    table is ON COMMIT DROP and the commit at the end of each file disposes of
    it.

    DISTINCT ON guards the one thing ON CONFLICT cannot survive: two rows for
    the same key inside a single push, which Postgres rejects with "cannot
    affect row a second time". counterTokenV2 is built so that cannot happen and
    it did not on 2026-08-26 (3,017,990 rows, all distinct), but a dropped
    duplicate shows up as rows_affected < rows_read rather than as a failed
    push, and run() prints both.
    """
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    read = 0
    with conn.cursor() as cur:
        # LIKE the real table so the staging columns keep its exact types, and
        # without its constraints so a duplicate inside this push reaches the
        # DISTINCT ON below instead of failing the COPY.
        cur.execute(f'CREATE TEMP TABLE "{_TEMP_TABLE}" (LIKE "{schema}"."{table}") ON COMMIT DROP')

        copy_sql = f'COPY "{_TEMP_TABLE}" ({quoted_cols}) FROM STDIN WITH (FORMAT csv, HEADER true)'
        with cur.copy(copy_sql) as copy:
            for rows in batches:
                if not rows:
                    continue
                copy.write(_batch_csv(columns, rows, header=(read == 0)))
                read += len(rows)

        if read == 0:
            conn.rollback()
            return 0, 0

        cur.execute(_upsert_sql(schema, table, columns))
        affected = cur.rowcount
    conn.commit()
    return read, affected


def push(cfg: config.PostgresPluginCfg, plugin_files) -> List[dict]:
    """Upsert plugin Parquet files into a sink's table; returns one result per file.

    Called by the load stage's postgres-plugin sink, which builds the files for
    its one market first and supplies the config (see premarketv6/load.py).
    Every problem raises: a load that could not push must not report success.
    """
    if not cfg.database_url:
        raise ValueError("postgres-plugin sink: database_url is not set")
    if not cfg.schema or not cfg.table:
        raise ValueError("postgres-plugin sink: schema and table must both be set")
    plugin_files = list(plugin_files)
    if not plugin_files:
        print(f"  No plugin rows to push to {cfg.schema}.{cfg.table}")
        return []
    results = []
    print(f"  Appending to {cfg.schema}.{cfg.table}...")
    with psycopg.connect(cfg.database_url) as conn:
        _ensure_table(conn, cfg.schema, cfg.table, cfg.create_table)
        for path in plugin_files:
            batches = _Pushable(parquet_export.iter_rows(path))
            read, affected = _copy_upsert(
                conn, cfg.schema, cfg.table, build.PLUGIN_COLUMNS, batches)
            note = "" if read == affected else f" ({read - affected} duplicate key(s) collapsed)"
            if batches.dropped:
                kinds = ", ".join(f"{e} {t}" for e, t in sorted(NOT_PUSHED))
                note += f" ({batches.dropped} {kinds} row(s) not pushed)"
            if batches.rescaled:
                note += f" ({batches.rescaled} row(s) in feed price units"
                note += f", {batches.inexact} strike(s) rounded)" if batches.inexact else ")"
            if batches.spot_named:
                note += f" ({batches.spot_named} spot fullname(s) prefixed 'SPOT ')"
            print(f"    Upserted {affected} rows from {path.name}{note}")
            results.append({"file": path, "read": read, "upserted": affected})
    return results
