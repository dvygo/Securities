"""Plugin normalization: map each canonical normalized CSV to the legacy pg
symbol-master schema (docs/plugin/pg_data_types.txt), one output file per
input file, written to data/YYYYMMDD/v6/plugin/ (sibling of normalized/)."""
import csv
import os
from datetime import datetime, timezone

import pandas as pd

from .. import bin_export, export, paths, runner

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
        # counterToken where the normalizer assigned one (the Databento venues),
        # otherwise the venue's own scriptToken. It is taken verbatim -- the
        # collision-free numbering lives in normalize/databento_norm.py now, so
        # this step no longer renumbers anything.
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

    print("  Building plugin CSVs...")
    csv_files = export.normalized_csv_files(opts.date_dir)
    if not csv_files:
        print("    No normalized CSVs found")
        return

    trade_date = f"{opts.date_dir[0:4]}-{opts.date_dir[4:6]}-{opts.date_dir[6:8]}"
    plugin_dir = paths.plugin_dir(opts.date_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)

    for csv_path in csv_files:
        exchange = csv_path.name.split("-", 1)[0]
        output_path = plugin_dir / csv_path.name
        # Staged the same way as the normalizer and the download side: PID-scoped so
        # concurrent runs cannot share a path, and NOT ending in .csv, because
        # postgres_export_plugin globs plugin/*.csv -- a leftover that still looked
        # like a finished file is what got pushed twice and broke the (token,
        # trade_date) primary key.
        temp_path = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")

        # Chunked read + append, so an --all-symbols venue is never held whole:
        # XCME is 961k rows and OPRA 2.03M, which previously became a source frame,
        # a list of mapped dicts and an output frame before a single write.
        total = 0
        try:
            with open(temp_path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=PLUGIN_COLUMNS, extrasaction="ignore", restval="")
                writer.writeheader()
                for frame in pd.read_csv(
                    csv_path, keep_default_na=False, dtype=str, chunksize=PLUGIN_CHUNK_ROWS
                ):
                    batch = [
                        map_row(row, trade_date, exchange)
                        for row in frame.to_dict("records")
                        if row.get("scriptToken")
                    ]
                    if not batch:
                        continue
                    writer.writerows(batch)
                    fh.flush()
                    total += len(batch)
        except Exception as e:
            print(f"    Error processing {csv_path}: {e}")
            temp_path.unlink(missing_ok=True)
            continue

        # An empty venue still gets a header-only file: unlike the raw/normalized
        # stages, a plugin CSV with no rows is a valid "nothing to push today".
        paths.promote_staging(temp_path, output_path)
        print(f"    Wrote {total} rows to {output_path}")
        bin_export.write_companion_safe(output_path)
