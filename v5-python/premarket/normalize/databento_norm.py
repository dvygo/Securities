"""Databento normalization: GLBX/OPRA/EQUS symbol parsing and row mapping."""
import re
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from .. import config, paths, runner
from . import price, session


# CME month character to month number mapping (for weekly expiries)
CME_MONTHS = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# OCC option symbol regex for parsing
OCC_REGEX = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def parse_occ_symbol(symbol: str) -> Dict[str, Any]:
    """Parse OCC option symbol format: AAAA YYMMDD C/P 8-digit-strike."""
    match = OCC_REGEX.match(symbol)
    if not match:
        return {}

    underlying, expiry_str, option_type, strike_str = match.groups()

    try:
        year = int(expiry_str[:2])
        month = int(expiry_str[2:4])
        day = int(expiry_str[4:6])

        # Two-digit year: 00-99, assume 00-30 = 2000-2030, 31-99 = 1931-1999
        full_year = 2000 + year if year <= 30 else 1900 + year

        strike = int(strike_str) / 1000.0  # Strike is encoded as integer (divide by 1000)

        return {
            "underlying": underlying,
            "expiration": f"{full_year:04d}{month:02d}{day:02d}",
            "option_type": "CALL" if option_type == "C" else "PUT",
            "strike": strike,
        }
    except (ValueError, IndexError):
        return {}


def glbx_strike_int(symbol: str) -> int:
    """Extract strike price as integer from GLBX symbol (ES weekday contract)."""
    # GLBX format: ES[Z23]P5500  (ZZ for week/quarter, then P/C for call/put, then strike)
    # This is simplified; actual extraction is complex
    match = re.search(r"([PC])(\d+)$", symbol)
    if match:
        try:
            return int(match.group(2))
        except ValueError:
            pass
    return 0


def glbx_expiration_yyyymmdd(symbol: str) -> str:
    """Extract expiration date as YYYYMMDD from GLBX symbol."""
    # GLBX format: ESH4 (March), ESM4 (June), etc.
    # Simplified extraction
    match = re.search(r"ES([A-Z])(\d{1,2})$", symbol)
    if match:
        month_char, year = match.groups()
        month = CME_MONTHS.get(month_char)
        if month:
            year_full = 2000 + int(year) if int(year) < 50 else 1900 + int(year)
            return f"{year_full:04d}{month:02d}01"  # Simplified to month start
    return ""


def underlying_root_from_stype_in(symbol: str) -> str:
    """Extract underlying root from raw symbol."""
    # For ES futures, root is "ES"
    # For SPX options, root is "SPX"
    if symbol.startswith("."):
        return symbol[1:]  # Remove leading dot for index symbols
    return re.match(r"^[A-Z]+", symbol).group(0) if re.match(r"^[A-Z]+", symbol) else symbol


def map_databento_row(row: Dict[str, Any], venue: str) -> Dict[str, Any]:
    """Map a Databento symbology record to canonical schema."""
    result = {}

    # Basic fields from DBN record
    stype_in = row.get("stype_in_symbol", "") or row.get("stype_in", "")
    stype_out = row.get("stype_out_symbol", "") or row.get("stype_out", "")

    result["script"] = stype_out or stype_in
    result["scriptToken"] = row.get("instrument_id", 0)
    result["scriptDetails"] = stype_out or stype_in

    # Currency
    result["currency"] = "USD"

    # Venue-specific processing
    if venue == "GLBX" or venue.startswith("XCME"):
        result["exchange"] = "XCME"
        result["scriptInstrumentType"] = "FUTURE"  # GLBX is futures-only
        result["underlying_root"] = underlying_root_from_stype_in(stype_in)
        result["underlying"] = underlying_root_from_stype_in(stype_in)
        result["strike"] = 0
        result["optionType"] = ""
        result["multiplier"] = 100000  # ES multiplier
        result["tickSize"] = 25  # ES tick size
        result["lotSize"] = 1
        result["tradingSessionUTC"] = session.trading_session_for_xcme()

    elif venue == "OPRA" or venue.startswith("XCBO"):
        result["exchange"] = "XCBO"
        result["scriptInstrumentType"] = "OPTION"
        parsed = parse_occ_symbol(stype_out or stype_in)
        if parsed:
            result["underlying"] = parsed.get("underlying", "")
            result["underlying_root"] = parsed.get("underlying", "").upper()
            result["strike"] = price.scale_price(parsed.get("strike", 0))
            result["optionType"] = "C" if parsed.get("option_type") == "CALL" else "P"
            result["expiration"] = int(datetime.strptime(parsed.get("expiration", "20240101"), "%Y%m%d").timestamp()) * 10**9
        else:
            result["underlying"] = ""
            result["underlying_root"] = ""
            result["strike"] = 0
            result["optionType"] = ""
            result["expiration"] = 0
        result["multiplier"] = 100000  # OPRA standard
        result["tickSize"] = price.scale_price(0.01)
        result["lotSize"] = 100
        result["tradingSessionUTC"] = session.trading_session_for_xcbo_equity()

    elif venue == "EQUS" or venue.startswith("XNAS"):
        result["exchange"] = "XNAS"
        result["scriptInstrumentType"] = "EQUITY"
        result["underlying_root"] = underlying_root_from_stype_in(stype_in)
        result["underlying"] = stype_in
        result["strike"] = 0
        result["optionType"] = ""
        result["multiplier"] = 1
        result["tickSize"] = price.scale_price(0.01)
        result["lotSize"] = 1
        result["expiration"] = 0  # No expiration for equities
        result["tradingSessionUTC"] = session.trading_session_for_xnas()

    # Fill in missing columns
    for col in paths.NORMALIZED_COLUMNS:
        if col not in result:
            result[col] = ""

    return result


def run(opts: runner.Opts) -> None:
    """Normalize Databento data: read CSV, map symbols, write normalized CSVs."""
    if opts.dry_run:
        print("DRY RUN: Would normalize Databento data")
        return

    print("  Normalizing Databento data...")
    normalized_dir = paths.normalized_dir(opts.date_dir)
    raw_dir = paths.raw_dir(opts.date_dir)

    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Process each Databento venue
    venues = ["xcme", "xcbo", "xnas"]
    for venue in venues:
        for mode in ["hist", "live"]:
            csv_path = raw_dir / f"databento_{venue}_{mode}.csv"
            if not csv_path.exists():
                continue

            print(f"    Processing {venue} {mode}...")

            try:
                df = pd.read_csv(csv_path)
                rows = []

                for _, row in df.iterrows():
                    norm_row = map_databento_row(row.to_dict(), venue.upper())
                    if norm_row.get("script"):
                        rows.append(norm_row)

                # Write normalized CSV
                output_path = normalized_dir / f"databento_{venue}_{mode}_normalized.csv"
                if rows:
                    out_df = pd.DataFrame(rows)
                    out_df = out_df[paths.NORMALIZED_COLUMNS]
                    out_df.to_csv(output_path, index=False, encoding="utf-8-sig")
                    print(f"      Wrote {len(out_df)} rows")
            except Exception as e:
                print(f"      Error processing {csv_path}: {e}")
