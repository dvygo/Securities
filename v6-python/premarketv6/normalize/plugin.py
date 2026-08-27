"""Plugin normalization: map each canonical normalized CSV to the legacy pg
symbol-master schema (docs/plugin/pg_data_types.txt), one output file per
input file, written to data/YYYYMMDD/v6/plugin/ (sibling of normalized/)."""
from datetime import datetime, timezone


from .. import export, parquet_export, paths, runner

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


# A nanosecond timestamp that cannot be real. int64 nanoseconds run out in 2262,
# so anything at or above this is a sentinel, not a date. Databento leaves an
# unset timestamp as UINT64_MAX on the wire rather than 0 or blank: on
# 2026-08-26 that is every one of the 13,201 XNAS equities and the 6,299 OPRA
# SPOT reference legs. Read literally it is the year 586524, which would sail
# past any "expired?" test and land in expirydate as 18446744073.
NS_SENTINEL_FLOOR = 2 ** 63


def _as_ns(value) -> int:
    """Parse a nanosecond-since-epoch field; 0 for missing, unparseable or unset.

    int() before float(): float cannot hold UINT64_MAX exactly, so the sentinel
    round-trips to 18446744073709551616 and no longer equals itself. float stays
    as the fallback because pandas widens an int column to float when any value
    is missing, which renders ids and timestamps as "1787616000000000000.0".
    """
    if value in (None, ""):
        return 0
    try:
        ns = int(value)
    except (TypeError, ValueError):
        try:
            ns = int(float(value))
        except (TypeError, ValueError):
            return 0
    return 0 if ns < 0 or ns >= NS_SENTINEL_FLOOR else ns


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
    present-but-unset (UINT64_MAX, see NS_SENTINEL_FLOOR) for every XNAS equity
    and every OPRA SPOT leg.

    0 from both is the genuine never-expires case -- equities and OPRA's SPOT
    reference legs, 42,275 rows on 2026-08-26 -- and must be kept. Treating a 0
    as "expired long ago" would delete every XNAS equity in the file.
    """
    return _as_ns(row.get("def_expiration")) or _as_ns(row.get("expiration"))


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


def map_row(row: dict, trade_date: str, exchange: str) -> dict:
    """Map one canonical normalized row (paths.NORMALIZED_COLUMNS) to the plugin/pg schema."""
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

    return {
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
        "ticksize": row.get("tickSize", ""),
        "name": row.get("script", ""),
        "series": series,
        "divisor": row.get("multiplier", ""),
        "exch": exchange,
        "fullname": _fullname(
            inst_type2, row.get("underlying", ""), row.get("strike", 0),
            row.get("multiplier", ""), opt_code, expiry_str,
        ),
        "freeze_qty": "",  # not carried by the canonical schema
    }


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
    plugin_dir = paths.plugin_dir(opts.date_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for src_path in normalized:
        exchange = src_path.name.split("-", 1)[0]
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
