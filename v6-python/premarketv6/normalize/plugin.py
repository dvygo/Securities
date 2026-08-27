"""Plugin normalization: map each canonical normalized CSV to the legacy pg
symbol-master schema (docs/plugin/pg_data_types.txt), one output file per
input file, written to data/YYYYMMDD/v6/plugin/ (sibling of normalized/)."""
from datetime import datetime, timezone


from .. import config, export, parquet_export, paths, runner
from . import session

# Column order matches docs/plugin/pg_data_types.txt exactly.
PLUGIN_COLUMNS = [
    "trade_date", "segment", "token", "symbol", "expirydate", "insttype",
    "optiontype", "strikeprice", "lotmultiple", "lotsize", "ticksize",
    "name", "series", "divisor", "exch", "fullname", "freeze_qty",
]

# scriptInstrumentType2 -> NSE-style segment label. Only FUTURE/OPTION/EQUITY
# are distinguished (the only cases in docs/plugin/sample.txt); anything else
# (INDEX, MF, warrants, ...) is left blank rather than guessed.
SEGMENT_BY_TYPE2 = {
    "FUTURE": "F&O",
    "OPTION": "F&O",
    "EQUITY": "CM",
}

# Normalized rows read and mapped before a batch is appended to the plugin CSV.
PLUGIN_CHUNK_ROWS = 50_000

# What every empty plugin value becomes. The plugin table types most of these
# columns as float8/int4, and an empty CSV field arrives there as NULL -- so
# lotmultiple, ticksize and freeze_qty, which the canonical schema does not
# carry at all, were landing as NULL on every Databento row (871,068 of 871,068
# for XCME on 2026-08-27). Filling them here means the pushed table has no NULL
# in any plugin column, whatever the venue.
#
# Applied to every column rather than a numeric subset, so a column added to
# PLUGIN_COLUMNS later cannot reintroduce a NULL by being forgotten here. "1"
# rather than "0" was chosen deliberately: these end up in divisor-like and
# multiplier-like positions where a zero is worse than a wrong-but-harmless one.
NULL_FILL = "1"

# Columns whose empty value is not NULL_FILL. Each is a placeholder that says
# "not applicable here" in the column's own vocabulary rather than in a generic
# one, so a consumer can tell an absent value from a real 1.
#
#   strikeprice  -1, because 0 is a plausible-looking strike and would be read
#                as one. Note this column is never empty -- it is 0 for anything
#                without a strike (210,376 XCME rows on 20260827) -- so the
#                substitution is keyed on 0 as well, in _plugin_placeholder.
#   optiontype   XX, the same not-an-option marker `series` already uses for
#                futures, instead of inventing a second one.
#   segment      F&O. Only the blanks are filled: SEGMENT_BY_TYPE2 leaves the
#                spread types empty and those are derivatives, but EQUITY maps
#                to CM and must stay CM (3,143 XNSE and 926 XBOM rows on
#                20260827, and every XNAS equity).
COLUMN_FILL = {
    "strikeprice": "-1",
    "optiontype": "XX",
    "segment": "F&O",
}

# strikeprice carries 0 for anything with no strike, so 0 is a missing value
# there and not a real one. Everywhere else 0 is meaningful and is left alone --
# expirydate 0 means "never expires".
ZERO_IS_MISSING = ("strikeprice",)

# Databento's undefined-price marker. Distinct from the undefined-TIMESTAMP one
# session.as_ns handles (UINT64_MAX): prices are signed, so an unset price is
# INT64_MAX and slips straight through a 2**63 floor. On 20260827 XCME,
# def_min_price_increment is this on 110,662 rows, a real value on 902,608 and
# zero on 35,328.
UNDEF_PRICE = 2 ** 63 - 1


def _expiry_ns(row: dict) -> int:
    """Effective expiry in nanoseconds since epoch UTC. 0 means never expires.

    def_expiration FIRST, because it is the venue's own last eligible trade
    time and canonical `expiration` is not trustworthy for XCME. The GLBX
    mapper derives expiration by regex off the symbol's month code
    (databento_norm.glbx_expiration_ns), and the symbol carries no day, so the
    day is hardcoded to the 1st of the month: all 622,952 XCME rows with a
    non-zero expiration land on day 1, while their real def_expiration days
    spread across the 25th-29th. It also mis-infers the decade near the wrap
    boundary -- 6AH1 normalizes to 2021-03-01 against a real 2031-03-17.

    Deriving the strip from that column deleted 67,201 XCME and 120 XCBO
    contracts that were still live, because "1st of the month" falls before a
    cutoff the real expiry sits after. Databento hands us the authoritative
    value in the definition record; use it.

    Canonical `expiration` second, as the fallback for rows where the venue
    gives us nothing: def_expiration is blank for the three Fyers venues, and
    present-but-unset (UINT64_MAX, see session.NS_SENTINEL_FLOOR) for every XNAS equity
    and every OPRA SPOT leg.

    0 from both is the genuine never-expires case -- equities and OPRA's SPOT
    reference legs, 42,275 rows on 2026-08-26 -- and must be kept. Treating a 0
    as "expired long ago" would delete every XNAS equity in the file.
    """
    return session.as_ns(row.get("def_expiration")) or session.as_ns(row.get("expiration"))


def _expiry_seconds(row: dict) -> int:
    """Effective expiry as seconds, which is what pg's expirydate holds."""
    return _expiry_ns(row) // 1_000_000_000


def _cutoff_ns(date_dir: str) -> int:
    """Strip boundary: 00:00 UTC on the run's own trade date.

    Anchored to date_dir and not to wall-clock now, so re-running an old day
    strips exactly what it stripped the first time.

    Contracts expiring ON the trade date are kept -- 0DTE options are live and
    heavily traded during the session the snapshot describes (28,213 rows on
    2026-08-26), so the boundary is the start of the day, not its end.
    """
    day = datetime.strptime(date_dir, "%Y%m%d").replace(tzinfo=timezone.utc)
    return int(day.timestamp()) * 1_000_000_000


def _is_expired(row: dict, cutoff_ns: int) -> bool:
    """True if this contract had already expired before the trade date began."""
    ns = _expiry_ns(row)
    return 0 < ns < cutoff_ns


def _ticksize(row: dict) -> str:
    """Tick size, preferring the venue's own min_price_increment.

    The canonical tickSize is blank for every Databento venue -- XCME sets it
    deliberately ("exclusive to the interactive layer"), so all 871,068 rows
    were falling through to a placeholder. The definition record carries the
    real value in def_min_price_increment.

    Kept in Databento's 1e-9 fixed point rather than divided out, matching what
    the neighbouring columns already do: strikeprice is fixed point at the same
    scale (665000000000 for a 665.0 strike) and divisor carries the scale
    itself. Dividing only this one column would make it the odd value out.

    Rows with no increment carry UNDEF_PRICE and fall through to the column
    placeholder, as do the 35,328 that carry a plain 0.
    """
    canonical = row.get("tickSize", "")
    if isinstance(canonical, str) and canonical.strip():
        return canonical
    if canonical not in (None, ""):
        return canonical
    return str(_price_value(row.get("def_min_price_increment")) or "")


def _price_value(value) -> int:
    """A Databento fixed-point price field as int; 0 for unset or unparseable."""
    try:
        price = int(value) if value not in (None, "") else 0
    except (TypeError, ValueError):
        try:
            price = int(float(value))
        except (TypeError, ValueError):
            return 0
    return 0 if price <= 0 or price >= UNDEF_PRICE else price


def _fullname(inst_type2: str, underlying: str, strike, divisor, opt_code: str, expiry_str: str) -> str:
    if inst_type2 == "OPTION":
        try:
            strike_display = (
                f"{int(float(strike)) / int(float(divisor)):.2f}"
                if divisor not in (None, "", 0, "0") else str(strike)
            )
        except (TypeError, ValueError, ZeroDivisionError):
            strike_display = str(strike)
        return f"OPT {underlying} {strike_display} {opt_code} {expiry_str}".strip()
    if inst_type2 == "FUTURE":
        return f"FUT {underlying}  {expiry_str}".rstrip()
    return underlying


def _is_missing(column: str, value) -> bool:
    """Whether this column's value counts as absent.

    None and "" both count: the canonical schema uses "" for a column a venue
    does not carry, and .get() returns None for one that is missing entirely.
    Whitespace-only counts too, since " " reaches Postgres as a non-NULL no more
    useful than a NULL. Zero counts only for the columns in ZERO_IS_MISSING.
    """
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if column in ZERO_IS_MISSING:
        try:
            return float(value) == 0
        except (TypeError, ValueError):
            return False
    return False


def _fill_nulls(mapped: dict) -> dict:
    """Replace every absent value with its column's placeholder.

    No plugin column is left empty: they are float8/int4 in the plugin table and
    an empty CSV field arrives through COPY as NULL.
    """
    return {
        k: (COLUMN_FILL.get(k, NULL_FILL) if _is_missing(k, v) else v)
        for k, v in mapped.items()
    }


def map_row(row: dict, trade_date: str, exchange: str) -> dict:
    """Map one canonical normalized row (paths.NORMALIZED_COLUMNS) to the plugin/pg schema.

    No value in the returned row is ever empty -- see _fill_nulls.
    """
    inst_type2 = row.get("scriptInstrumentType2", "")
    option_type = row.get("optionType", "")
    opt_code = "CE" if option_type == "CALL" else "PE" if option_type == "PUT" else ""

    if inst_type2 == "FUTURE":
        series = "XX"
    elif opt_code:
        series = opt_code
    else:
        series = ""

    expiry_sec = _expiry_seconds(row)
    expiry_str = datetime.fromtimestamp(expiry_sec, tz=timezone.utc).strftime("%Y-%m-%d") if expiry_sec else ""

    return _fill_nulls({
        "trade_date": trade_date,
        "segment": SEGMENT_BY_TYPE2.get(inst_type2, ""),
        # counterTokenV2, taken verbatim -- the collision-free numbering lives in
        # normalize/counter_token.py (driven by databento_norm.py for the
        # Databento venues and fields.py for the Fyers ones), so this step no
        # longer renumbers anything.
        #
        # V2 and not counterToken: the pg symbol-master keys on
        # (token, trade_date), so the token is what any cross-date join resolves
        # against. counterToken is POSITIONAL -- a script's number moves whenever
        # the day's row order shifts, so yesterday's token silently names a
        # different script today. V2 is stable: a script keeps its number for as
        # long as it keeps appearing, and a number is only reused once its script
        # stops appearing.
        #
        # The fallbacks are a guard, not a path any venue takes today -- all six
        # are fully numbered on both columns. But VenueTokens.token() returns ""
        # for a script with no V2 allocation, and an empty token would collide on
        # the primary key, so fall back to the positional token before the
        # venue's own id.
        "token": row.get("counterTokenV2") or row.get("counterToken") or row.get("scriptToken", ""),
        "symbol": row.get("underlying_root", ""),
        "expirydate": str(expiry_sec),
        "insttype": row.get("scriptInstrumentType", ""),
        "optiontype": opt_code,
        "strikeprice": row.get("strike", ""),
        "lotmultiple": "",  # not carried by the canonical schema
        "lotsize": row.get("lotSize", ""),
        "ticksize": _ticksize(row),
        "name": row.get("script", ""),
        "series": series,
        "divisor": row.get("multiplier", ""),
        "exch": exchange,
        "fullname": _fullname(
            inst_type2, row.get("underlying", ""), row.get("strike", 0),
            row.get("multiplier", ""), opt_code, expiry_str,
        ),
        "freeze_qty": "",  # not carried by the canonical schema
    })


def run(opts: runner.Opts) -> None:
    """Build plugin CSVs: mirror each normalized CSV into data/YYYYMMDD/v6/plugin/ under the legacy pg schema."""
    if opts.dry_run:
        print("DRY RUN: Would build plugin CSVs")
        return

    print("  Building plugin files...")
    normalized = export.normalized_files(opts.date_dir)
    if not normalized:
        print("    No normalized files found")
        return

    trade_date = f"{opts.date_dir[0:4]}-{opts.date_dir[4:6]}-{opts.date_dir[6:8]}"
    cutoff_ns = _cutoff_ns(opts.date_dir)
    exchanges = config.load_exchanges()
    plugin_dir = paths.plugin_dir(opts.date_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for src_path in normalized:
        exchange = src_path.name.split("-", 1)[0]
        # A disabled venue is skipped even when a normalized file is sitting
        # there: the file is yesterday's, left behind by the stage that stopped
        # writing it, and building a plugin file from it would push stale rows
        # under today's trade_date.
        if not runner.venue_selected(opts, exchange):
            continue
        venue_cfg = exchanges.get(exchange.lower())
        if venue_cfg is not None and not venue_cfg.enabled:
            print(f"    Skipping {exchange}: enabled = 0")
            continue
        output_path = plugin_dir / src_path.name
        # Staging and promote-on-close live in RowWriter: PID-scoped so concurrent
        # runs cannot share a path, and never under the finished name -- a leftover
        # that looked finished is what got pushed twice and broke the
        # (token, trade_date) primary key.
        #
        # Chunked read + write, so an --all-symbols venue is never held whole:
        # XCME is 961k rows and OPRA 2.03M, which previously became a source frame,
        # a list of mapped dicts and an output frame before a single write.
        writer = parquet_export.RowWriter(output_path, PLUGIN_COLUMNS)
        total = 0
        expired = 0
        try:
            for rows in parquet_export.iter_rows(src_path, PLUGIN_CHUNK_ROWS):
                # Expired contracts are dropped here rather than mapped and
                # filtered later: the symbol master is what the plugin resolves
                # against, and a contract that stopped trading before this day
                # began cannot be the answer to any lookup for it.
                live = []
                for row in rows:
                    if not row.get("scriptToken"):
                        continue
                    if _is_expired(row, cutoff_ns):
                        expired += 1
                        continue
                    live.append(row)
                batch = [map_row(row, trade_date, exchange) for row in live]
                if not batch:
                    continue
                writer.write(batch)
                total += len(batch)
        except Exception as e:
            print(f"    Error processing {src_path}: {e}")
            writer.abort()
            continue

        # Unlike the raw/normalized stages, an empty venue here is a valid
        # "nothing to push today" -- but a row-group-less Parquet file is not
        # readable, so nothing is written and the push simply finds no file.
        dropped = f" ({expired:,} expired dropped)" if expired else ""
        if writer.close():
            print(f"    Wrote {total} rows to {output_path}{dropped}")
        else:
            print(f"    No plugin rows for {exchange}{dropped}")
