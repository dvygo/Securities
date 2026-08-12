"""Plugin normalization: map each canonical normalized CSV to the legacy pg
symbol-master schema (docs/plugin/pg_data_types.txt), one output file per
input file, written to data/YYYYMMDD/v6/plugin/ (sibling of normalized/)."""
from datetime import datetime, timezone

import pandas as pd

from .. import export, paths, runner

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

# Plugin tokens for the Databento venues are a per-venue counter, not the
# Databento instrument_id.
#
# The target pg symbol-master table keys on (token, trade_date) with no exchange
# column, and instrument_id is only unique WITHIN a dataset -- on 2026-08-12 the
# raw ids collide 932 times between XCME and XNAS, because EQUS ids start at 1 and
# run straight into GLBX's low ids. Numbering each venue into its own block makes
# that impossible by construction: the base digit keeps the venues apart and the
# 35000 floor keeps us clear of the ids already sitting in that externally-managed
# table.
#
# Databento venues only. Files from other sources (XNSE/XIMC/XBOM/...) keep the
# token their own pipeline assigned -- this does not renumber them.
#
# These tokens are positional and therefore per-day: the same contract gets a
# different number tomorrow if the universe shifts. That is intended, since the
# primary key includes trade_date. Nothing may join on token across dates.
PLUGIN_TOKEN_BASE = {"XNAS": "1", "XCBO": "2", "XCME": "3"}
PLUGIN_TOKEN_START = 35000


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
        "token": row.get("scriptToken", ""),
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
        try:
            df = pd.read_csv(csv_path, keep_default_na=False, dtype=str)
        except Exception as e:
            print(f"    Error reading {csv_path}: {e}")
            continue

        rows = [
            map_row(row.to_dict(), trade_date, exchange)
            for _, row in df.iterrows()
            if row.get("scriptToken")
        ]

        # Renumber the Databento venues into their own block (see PLUGIN_TOKEN_BASE).
        # Done after the scriptToken filter so the counter has no gaps.
        base = PLUGIN_TOKEN_BASE.get(exchange)
        if base:
            for i, r in enumerate(rows):
                r["token"] = f"{base}{PLUGIN_TOKEN_START + i}"

        output_path = plugin_dir / csv_path.name
        out_df = pd.DataFrame(rows, columns=PLUGIN_COLUMNS) if rows else pd.DataFrame(columns=PLUGIN_COLUMNS)
        out_df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"    Wrote {len(out_df)} rows to {output_path}")
