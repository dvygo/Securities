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

# OCC option symbol regex for parsing. Only the suffix is anchored (matches
# v4-golang's opraOCCTail): real Databento OPRA symbols space-pad the root
# to 6 chars (e.g. "NVDA  270115P00090000"), so anchoring the root at "^"
# with no whitespace allowance silently fails to match every row.
OCC_REGEX = re.compile(r"(\d{6})([CP])(\d{8})\s*$")

# GLBX weekly-option suffix, e.g. "ESZ7 P8250" / "ESM8 C9700" -> (P, 8250).
# Matches v4-golang's glbxCPStrike. Futures symbols (e.g. "ESZ7") never
# match this, so its absence is what identifies a plain future.
GLBX_OPTION_REGEX = re.compile(r"\s+([CP])(\d+(?:\.\d+)?)\s*$")

# US venues (XCME/XCBO) mirror Databento's own wire format: prices are
# fixed-point with a 1e-9 scale (e.g. a $1 price is the int64 1_000_000_000).
# Must be passed explicitly to price.scale_price -- its default scale is
# India's INDIA_PRICE_SCALE (1e5) for the Fyers/rupee path, not this one.
US_PRICE_SCALE = 10**9


def parse_occ_symbol(symbol: str) -> Dict[str, Any]:
    """Parse OCC option symbol format: AAAA YYMMDD C/P 8-digit-strike."""
    s = symbol.strip()
    match = OCC_REGEX.search(s)
    if not match:
        return {}

    expiry_str, option_type, strike_str = match.groups()
    underlying = s[: match.start()].strip().replace(" ", "").upper()

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


def map_databento_row(row: Dict[str, Any], venue: str, ref_date=None) -> Dict[str, Any]:
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
        result["underlying_root"] = underlying_root_from_stype_in(stype_in)
        result["underlying"] = underlying_root_from_stype_in(stype_in)
        result["multiplier"] = US_PRICE_SCALE  # matches Databento's 1e-9 fixed-point wire price scale
        result["tickSize"] = ""  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        result["lotSize"] = 1
        result["tradingSessionUTC"] = session.trading_session_for_xcme(ref_date)

        opt_match = GLBX_OPTION_REGEX.search(stype_out or stype_in)
        if opt_match:
            result["scriptInstrumentType"] = "OPTIDX"
            result["scriptInstrumentType2"] = "OPTION"
            result["optionType"] = "CALL" if opt_match.group(1) == "C" else "PUT"
            result["strike"] = price.scale_price(float(opt_match.group(2)), US_PRICE_SCALE)
        else:
            result["scriptInstrumentType"] = "FUTIDX"
            result["scriptInstrumentType2"] = "FUTURE"
            result["strike"] = 0
            result["optionType"] = ""

    elif venue == "OPRA" or venue.startswith("XCBO"):
        result["exchange"] = "XCBO"
        parsed = parse_occ_symbol(stype_out or stype_in)
        if parsed:
            underlying = parsed.get("underlying", "")
            result["underlying"] = underlying
            result["underlying_root"] = underlying.upper()
            result["strike"] = price.scale_price(parsed.get("strike", 0), US_PRICE_SCALE)
            result["optionType"] = parsed.get("option_type", "")
            result["expiration"] = int(datetime.strptime(parsed.get("expiration", "20240101"), "%Y%m%d").timestamp()) * 10**9
        else:
            underlying = ""
            result["underlying"] = ""
            result["underlying_root"] = ""
            result["strike"] = 0
            result["optionType"] = ""
            result["expiration"] = 0
        is_index = underlying.upper() in ("SPX", "SPXW", "VIX", "RUT")
        result["scriptInstrumentType"] = "OPTIDX" if is_index else "OPTSTK"
        result["scriptInstrumentType2"] = "OPTION"
        result["multiplier"] = US_PRICE_SCALE  # matches Databento's 1e-9 fixed-point wire price scale
        result["tickSize"] = ""  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        result["lotSize"] = 100
        result["tradingSessionUTC"] = (
            session.trading_session_for_xcbo_index(ref_date) if is_index else session.trading_session_for_xcbo_equity(ref_date)
        )

    elif venue == "EQUS" or venue.startswith("XNAS"):
        result["exchange"] = "XNAS"
        result["scriptInstrumentType"] = "EQUITY"
        result["scriptInstrumentType2"] = "EQUITY"
        result["underlying_root"] = underlying_root_from_stype_in(stype_in)
        result["underlying"] = stype_in
        result["strike"] = 0
        result["optionType"] = ""
        result["multiplier"] = US_PRICE_SCALE  # matches Databento's 1e-9 fixed-point wire price scale
        result["tickSize"] = ""  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        result["lotSize"] = 1
        result["expiration"] = 0  # No expiration for equities
        result["tradingSessionUTC"] = session.trading_session_for_xnas(ref_date)

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
    ref_date = datetime.strptime(opts.date_dir, "%Y%m%d").date()

    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Process each Databento venue
    venues = ["xcme", "xcbo", "xnas"]
    for venue in venues:
        csv_path = paths.databento_raw_csv(opts.date_dir, venue)
        if not csv_path.exists():
            continue

        print(f"    Processing {venue}...")

        try:
            df = pd.read_csv(csv_path)
            rows = []

            for _, row in df.iterrows():
                norm_row = map_databento_row(row.to_dict(), venue.upper(), ref_date)
                if norm_row.get("script"):
                    rows.append(norm_row)

            # Write normalized CSV
            output_path = normalized_dir / f"{venue.upper()}-DATABENTO-normalized.csv"
            if rows:
                out_df = pd.DataFrame(rows)
                out_df = out_df[paths.NORMALIZED_COLUMNS]
                out_df.to_csv(output_path, index=False, encoding="utf-8-sig")
                print(f"      Wrote {len(out_df)} rows")
        except Exception as e:
            print(f"      Error processing {csv_path}: {e}")
