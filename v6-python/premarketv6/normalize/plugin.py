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


def _expiry_seconds(expiration) -> int:
    """Canonical `expiration` is nanoseconds since epoch UTC; pg's expirydate is seconds."""
    try:
        ns = int(float(expiration)) if expiration not in (None, "") else 0
    except ValueError:
        return 0
    return ns // 1_000_000_000 if ns > 0 else 0


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

    expiry_sec = _expiry_seconds(row.get("expiration"))
    expiry_str = datetime.fromtimestamp(expiry_sec, tz=timezone.utc).strftime("%Y-%m-%d") if expiry_sec else ""

    return {
        "trade_date": trade_date,
        "segment": SEGMENT_BY_TYPE2.get(inst_type2, ""),
        # counterToken, taken verbatim -- the collision-free numbering lives in
        # normalize/counter_token.py now (driven by databento_norm.py for the
        # Databento venues and fields.py for the Fyers ones), so this step no
        # longer renumbers anything. Every venue is numbered, so the scriptToken
        # fallback is unreachable in practice; it is kept only so a row that
        # somehow arrives unnumbered still carries an id rather than an empty
        # token, which would collide on the (token, trade_date) primary key.
        "token": row.get("counterToken") or row.get("scriptToken", ""),
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
        try:
            for rows in parquet_export.iter_rows(src_path, PLUGIN_CHUNK_ROWS):
                batch = [
                    map_row(row, trade_date, exchange)
                    for row in rows
                    if row.get("scriptToken")
                ]
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
        if writer.close():
            print(f"    Wrote {total} rows to {output_path}")
        else:
            print(f"    No plugin rows for {exchange}")
