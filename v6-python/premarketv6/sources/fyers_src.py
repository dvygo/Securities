"""Fyers (Indian broker) symbol master integration."""
import csv
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

from .. import config, paths, runner


# Fyers sym_details field names in column order for headerless CSVs.
# This is the REAL on-wire column order (verified against a live BSE_CM.csv row);
# a previous, unrelated, fabricated column list here never matched the
# actual feed and silently misaligned every field.
FYERS_COLUMNS = [
    "fyToken", "symDetails", "exInstType", "minLotSize", "tickSize", "isin",
    "tradingSession", "lastUpdate", "expiryDate", "symTicker", "exchange",
    "segment", "exToken", "exSymName", "underExToken", "strikePrice",
    "optType", "underFyTok", "underSym", "fyersExtra1", "fyersExtra2",
]

# Legacy pre-v2 CSV header names -> canonical FYERS_COLUMNS key.
LEGACY_HEADER_ALIASES = {
    "fytoken": "fyToken",
    "symbol": "symDetails",
    "instrumenttype": "exInstType",
    "lotsize": "minLotSize",
    "isin": "isin",
    "symbolticker": "symTicker",
    "scriptcode": "exToken",
    "scripcode": "exToken",
    "scripname": "exSymName",
    "shortsym": "exSymName",
    "scriptoken": "underExToken",
    "optiontype": "optType",
    "underfytoken": "underFyTok",
    "underexsymbol": "underSym",
}

# Fyers API v3 appendix codes (https://myapi.fyers.in/docsv3#tag/Appendix).
EXCHANGE_CODES = {
    10: "NSE",
    11: "MCX",
    12: "BSE",
}

SEGMENT_CODES = {
    10: "CM",
    11: "FO",
    12: "CD",
    20: "COM",
}

INSTRUMENT_CODES = {
    0: "EQ", 1: "PREFSHARES", 2: "DEBENTURES", 3: "WARRANTS", 4: "MISC",
    5: "SGB", 6: "G-SECS", 7: "T-BILLS", 8: "MF", 9: "ETF", 10: "INDEX",
    11: "FUTIDX", 12: "FUTIVX", 13: "FUTSTK", 14: "OPTIDX", 15: "OPTSTK",
    16: "FUTCUR", 17: "FUTIRT", 18: "FUTIRC", 19: "OPTCUR", 20: "UNDCUR",
    21: "UNDIRC", 22: "UNDIRT", 23: "UNDIRD", 24: "INDEX_CD", 25: "FUTIRD",
    30: "FUTCOM", 31: "OPTFUT", 32: "OPTCOM", 33: "FUTBAS", 34: "FUTBLN",
    35: "FUTENR", 36: "OPTBLN", 37: "OPTFUT_NCOM", 50: "MISC_BSE",
}

# optType is already the literal string CE/PE/XX on the wire, not a numeric code.
OPTION_TYPE_NONE = "XX"
OPTION_TYPE_CE = "CE"
OPTION_TYPE_PE = "PE"

# (exchange_code, segment_code) -> pipeline MIC, matching v4's exchangeMIC.
_EXCHANGE_MIC = {
    (10, 10): "XNSE", (10, 11): "XNFO", (10, 12): "XNCD", (10, 20): "XNCO",
    (12, 10): "XBSE", (12, 11): "XBFO", (12, 12): "XBCD",
    (11, 20): "XMCX",
}


def normalize_header_key(key: str) -> str:
    """Normalize a CSV header cell to its canonical FYERS_COLUMNS key."""
    key = key.strip()
    lowered = key.lower()
    if lowered in LEGACY_HEADER_ALIASES:
        return LEGACY_HEADER_ALIASES[lowered]
    for col in FYERS_COLUMNS:
        if key == col or lowered == col.lower():
            return col
    return key


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
    """Map raw (exchange, segment) appendix codes, e.g. ("12", "10"), to pipeline MIC (e.g. "XBSE")."""
    try:
        return _EXCHANGE_MIC.get((int(exchange), int(segment)), "")
    except (ValueError, TypeError):
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
        except requests.RequestException:
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
