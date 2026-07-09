"""Fyers normalization: map raw rows to 16-column canonical schema."""
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .. import config, paths, runner
from ..sources import fyers_src
from . import price, session


# Instrument type classification
INST_TYPE_MAP = {
    "EQ": "EQUITY",
    "FUTCOM": "FUTURE",
    "OPTCOM": "OPTION",
    "FUTIDX": "FUTURE",
    "OPTIDX": "OPTION",
    "FUTSTK": "FUTURE",
    "OPTSTK": "OPTION",
    "OPTCUR": "OPTION",
    "FUTCUR": "FUTURE",
    "SPOTFWD": "EQUITY",
    "SPOTCUR": "EQUITY",
    "FUTIRT": "FUTURE",
    "OPTIRT": "OPTION",
    "SPOTIRT": "EQUITY",
    "SPOTGOLD": "EQUITY",
    "SPOTSILVER": "EQUITY",
    "MUTUALFUND": "EQUITY",
    "BOND": "EQUITY",
    "GOVT_BOND": "EQUITY",
    "ETF": "EQUITY",
    "SPOT": "EQUITY",
    "WARRANT": "WARRANT",
    "SPOTSLV": "EQUITY",
}


def classify_instrument(inst_code: str) -> tuple[str, str]:
    """
    Classify instrument type and subtype.
    Returns (scriptInstrumentType, scriptInstrumentType2).
    """
    main_type = INST_TYPE_MAP.get(inst_code, "UNKNOWN")

    # For options, add option type if available
    sub_type = inst_code if inst_code in ["CE", "PE"] else ""

    return main_type, sub_type


def map_fyers_row(row: Dict[str, str], cfg: config.NormalizerCfg) -> Dict[str, Any]:
    """
    Map a single Fyers raw row to the 16-column canonical schema.
    Returns dict with canonical columns (may be sparse).
    """
    result = {}

    # Basic identifiers
    result["script"] = row.get("symbol", "")
    result["scriptToken"] = row.get("fyToken", "")
    result["scriptDetails"] = row.get("description", "") or row.get("symbol", "")

    # Exchange/MIC resolution
    exchange = row.get("exchange", "")
    segment = row.get("segment", "")
    mic = fyers_src.resolve_exchange_mic(exchange, segment)
    result["exchange"] = mic or exchange

    # Instrument type classification
    inst_type = row.get("instrumenttype", "").upper()
    inst_type, opt_type = classify_instrument(inst_type)
    result["scriptInstrumentType"] = inst_type
    result["scriptInstrumentType2"] = opt_type
    result["optionType"] = opt_type or ""

    # ISIN
    result["ISIN"] = row.get("isin", "")

    # Price fields (scaled)
    tick_size = row.get("tick_size") or row.get("TickSize", "0")
    result["tickSize"] = price.scale_price(tick_size)

    strike = row.get("strike") or row.get("StrikPrice", "")
    result["strike"] = price.scale_price(strike) if strike else 0

    # Quantities
    lot_size = row.get("lot_size") or row.get("LotSize", "1")
    try:
        result["lotSize"] = int(lot_size) if lot_size else 1
    except ValueError:
        result["lotSize"] = 1

    # Multiplier (contract size)
    mult = row.get("mult") or row.get("multiplier", "1")
    try:
        result["multiplier"] = int(mult) if mult else 1
    except ValueError:
        result["multiplier"] = 1

    # Currency (default to INR for India)
    result["currency"] = "INR"

    # Underlying
    underlying = row.get("underlyingsymbol") or row.get("Underlying", "")
    result["underlying"] = underlying
    # Extract underlying root (remove exchange prefix if present)
    result["underlying_root"] = underlying.split(":")[-1] if underlying else ""

    # Trading session (IST to UTC conversion for Fyers)
    # Fyers doesn't provide session info in symbol master, use defaults
    session_str = "09:15-15:30"  # NSE/BSE regular session IST
    result["tradingSessionUTC"] = session.trading_session_ist_to_utc(session_str) or session_str

    # Expiration (convert Unix timestamp to nanoseconds UTC)
    expiry = row.get("expirydate") or row.get("ExpiryDate", "")
    if expiry:
        try:
            # Try parsing as Unix timestamp
            expiry_ts = int(float(expiry))
            result["expiration"] = expiry_ts * 10**9  # convert to nanoseconds
        except ValueError:
            # Try parsing as YYYYMMDD
            try:
                dt = datetime.strptime(str(expiry), "%Y%m%d")
                result["expiration"] = int(dt.timestamp()) * 10**9
            except ValueError:
                result["expiration"] = 0
    else:
        result["expiration"] = 0

    return result


def run(opts: runner.Opts) -> None:
    """Normalize Fyers data: read raw CSVs, map to canonical schema, write normalized CSVs."""
    if opts.dry_run:
        print("DRY RUN: Would normalize Fyers data")
        return

    cfg = config.load_normalizer()
    normalized_dir = paths.normalized_dir(opts.date_dir)
    raw_dir = paths.fyers_raw_dir(opts.date_dir)

    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Process each Fyers MIC bundle
    for mic, (output_csv, table_name, source_files) in paths.FYERS_MIC_BUNDLES.items():
        print(f"  Normalizing Fyers {mic}...")

        all_rows = []
        for source_file in source_files:
            source_path = raw_dir / source_file
            if not source_path.exists():
                continue

            raw_rows = fyers_src.parse_fyers_csv(source_path)
            for raw_row in raw_rows:
                norm_row = map_fyers_row(raw_row, cfg)
                # Filter out empty/invalid rows
                if norm_row.get("script") and norm_row.get("exchange"):
                    all_rows.append(norm_row)

        # Write normalized CSV
        output_path = normalized_dir / output_csv
        if all_rows:
            df = pd.DataFrame(all_rows)
            # Ensure all columns exist (fill missing with defaults)
            for col in paths.NORMALIZED_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            # Reorder to canonical columns
            df = df[paths.NORMALIZED_COLUMNS]
            df.to_csv(output_path, index=False, encoding="utf-8-sig")
            print(f"    Wrote {len(df)} rows to {output_path}")
        else:
            # Write empty CSV with headers
            df = pd.DataFrame(columns=paths.NORMALIZED_COLUMNS)
            df.to_csv(output_path, index=False, encoding="utf-8-sig")
            print(f"    No data for {output_path}")


def run_strip(opts: runner.Opts) -> None:
    """
    Strip/filter normalized data (e.g., near-term OPRA contracts).
    Placeholder for now; actual implementation in strip.py.
    """
    print("  Running strip filter...")
