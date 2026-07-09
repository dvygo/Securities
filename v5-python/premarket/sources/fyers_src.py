"""Fyers (Indian broker) symbol master integration."""
import csv
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .. import config, paths, runner


# Fyers columns (from internal/fyers/columns.go)
FYERS_COLUMNS = [
    "exchange", "segment", "symbol", "description", "series", "isin",
    "exch_token", "fyToken", "tick_size", "lot_size", "instrumenttype",
    "optiontype", "expirydate", "strike", "underlyingsymbol", "underlyingtoken",
    "mult", "contract_description", "contractsize", "buyqty", "sellqty",
]

# Legacy header aliases for older Fyers format
LEGACY_HEADER_ALIASES = {
    "exchange": "EXCH",
    "segment": "SEG",
    "symbol": "SYMBOL",
    "description": "DESC",
    "series": "SERIES",
    "isin": "ISIN",
    "exch_token": "EXCH_TOKEN",
    "fyToken": "FYTOKEN",
    "tick_size": "TickSize",
    "lot_size": "LotSize",
    "instrumenttype": "InstType",
    "optiontype": "OptionType",
    "expirydate": "ExpiryDate",
    "strike": "StrikPrice",
    "underlyingsymbol": "Underlying",
    "underlyingtoken": "UnderlyingToken",
    "mult": "multiplier",
}

# Fyers appendix codes (from internal/fyers/appendix.go)
EXCHANGE_CODES = {
    1: "NSE",
    2: "BSE",
    3: "MCX",
    4: "NCDEX",
}

SEGMENT_CODES = {
    1: "CM",
    2: "FO",
    3: "CD",
    4: "COM",
}

INSTRUMENT_CODES = {
    1: "EQ", 10: "FUTCOM", 11: "OPTCOM", 12: "FUTIDX", 13: "OPTIDX",
    14: "FUTSTK", 15: "OPTSTK", 16: "OPTCUR", 17: "FUTCUR", 18: "SPOTFWD",
    19: "SPOTCUR", 20: "FUTIRT", 21: "OPTIRT", 22: "SPOTIRT", 23: "SPOTGOLD",
    24: "SPOTSILVER", 25: "MUTUALFUND", 26: "BOND", 27: "GOVT_BOND",
    28: "ETF", 29: "SPOT", 30: "WARRANT", 31: "SPOTSLV",
}

OPTION_TYPES = {
    1: "CE",
    2: "PE",
}


def normalize_header_key(key: str) -> str:
    """Normalize header key to canonical name."""
    if key in LEGACY_HEADER_ALIASES.values():
        for canonical, legacy in LEGACY_HEADER_ALIASES.items():
            if legacy == key:
                return canonical
    return key.lower()


def parse_fy_token(fy_token: str) -> Dict[str, Any]:
    """
    Parse Fyers fyToken format: EE SS YYMMDD EXTOKEN
    Returns dict with exchange, segment, date, token.
    """
    if not fy_token or len(fy_token) < 9:
        return {}

    try:
        ex_code = int(fy_token[0:2])
        seg_code = int(fy_token[2:4])
        date_str = fy_token[4:10]
        ex_token = fy_token[10:]

        exchange = EXCHANGE_CODES.get(ex_code, "")
        segment = SEGMENT_CODES.get(seg_code, "")

        return {
            "exchange": exchange,
            "segment": segment,
            "date": date_str,
            "token": ex_token,
        }
    except (ValueError, IndexError):
        return {}


def resolve_exchange_mic(exchange: str, segment: str) -> str:
    """Map (exchange, segment) to pipeline MIC."""
    if exchange == "NSE":
        if segment == "CM":
            return "XNSE"
        elif segment == "FO":
            return "XNFO"
        elif segment == "CD":
            return "XNCD"
    elif exchange == "BSE":
        if segment == "CM":
            return "XBSE"
        elif segment == "FO":
            return "XBFO"
    elif exchange == "MCX" or exchange == "NCDEX":
        return "XIMC"
    return ""


def is_cash_instrument(inst_type: str) -> bool:
    """Check if instrument is a cash/spot instrument."""
    return inst_type in ["EQ", "SPOTFWD", "SPOTCUR", "SPOTIRT", "SPOTGOLD", "SPOTSILVER", "MUTUALFUND", "BOND", "ETF", "SPOT", "SPOTSLV"]


def is_future(inst_type: str) -> bool:
    """Check if instrument is a futures contract."""
    return "FUT" in inst_type


def is_option(inst_type: str) -> bool:
    """Check if instrument is an options contract."""
    return "OPT" in inst_type


def download(opts: runner.Opts) -> None:
    """Download Fyers Indian exchange symbol master CSVs."""
    cfg = config.load_fyers()

    if opts.dry_run:
        print("DRY RUN: Would download Fyers symbol master data")
        return

    raw_dir = paths.fyers_raw_dir(opts.date_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Segment key -> source file mapping (from v4 Python)
    segment_sources = {
        "xnse": "NSE_CM.csv",
        "xnfo": "NSE_FO.csv",
        "xncd": "NSE_CD.csv",
        "xbse": "BSE_CM.csv",
        "xbfo": "BSE_FO.csv",
        "xmcx": "MCX_COM.csv",
    }

    # Download each segment
    for segment, source_file in segment_sources.items():
        filename = paths.FYERS_RAW_SEGMENTS.get(segment)
        if not filename:
            continue

        url = f"{cfg.base_url}/{source_file}"
        output_path = raw_dir / filename

        print(f"Downloading {segment}...")
        try:
            csv_data = fetch_with_retry(url, cfg)
            output_path.write_text(csv_data, encoding="utf-8-sig")
            print(f"  Wrote {output_path}")
        except Exception as e:
            print(f"  Error downloading {segment}: {e}")


def fetch_with_retry(url: str, cfg: config.FyersCfg, max_retries: int = 3) -> str:
    """Fetch URL with retry logic."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": cfg.user_agent},
                timeout=cfg.timeout_sec,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(cfg.retry_delay_sec)
                continue
            raise


def read_raw_csv(path: Path) -> List[Dict[str, str]]:
    """Read raw Fyers CSV (headerless or headered, old or new format)."""
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8-sig").strip().split("\n")
    if not lines:
        return []

    # Detect if first line is a header
    first_line_parts = lines[0].split(",")
    has_header = any(col.lower() in LEGACY_HEADER_ALIASES for col in first_line_parts)

    rows = []
    reader = csv.DictReader(
        lines,
        fieldnames=None if has_header else FYERS_COLUMNS,
    )

    for row in reader:
        if not row or not any(row.values()):
            continue
        rows.append(row)

    return rows


def parse_fyers_csv(path: Path) -> List[Dict[str, str]]:
    """Parse Fyers CSV and normalize columns."""
    rows = read_raw_csv(path)
    normalized = []

    for row in rows:
        # Normalize header keys
        norm_row = {normalize_header_key(k): v for k, v in row.items()}
        normalized.append(norm_row)

    return normalized
