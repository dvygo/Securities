"""Shared symbology paths — ``exchange.vendor`` naming (MIC-VENDOR)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

_HELPERS_DIR = Path(__file__).resolve().parent
_V4_DIR = _HELPERS_DIR.parent

RAW_SUBDIR = "raw"
NORMALIZED_SUBDIR = "normalized"
CONF_SUBDIR = "conf"

POSTGRES_SCHEMA_PREFIX = "v2-"

NORMALIZED_COLUMNS: tuple[str, ...] = (
    "date",
    "exchange",
    "underlying_root",
    "underlying",
    "strike",
    "expiration",
    "multiplier",
    "token",
    "symbol",
)

XCME_CSV = "XCME-DATABENTO.csv"
XCBO_CSV = "XCBO-DATABENTO.csv"
XNAS_CSV = "XNAS-DATABENTO.csv"

XCME_SCRIPT = "XCME-DATABENTO.py"
XCBO_SCRIPT = "XCBO-DATABENTO.py"
XNAS_SCRIPT = "XNAS-DATABENTO.py"

UNDERLYINGS_CSV = "XNAS-XCBOE-underlyings.csv"
SECRETS_INI = "secrets.ini"
SECRETS_DIRNAME = "secrets"

FYERS_BASE_URL = "https://public.fyers.in/sym_details"
FYERS_VENDOR = "FYERS"

FYERS_RAW_COLUMNS: tuple[str, ...] = (
    "fytoken",
    "symbol",
    "instrumentType",
    "lotSize",
    "tickSize",
    "ISIN",
    "tradingSession",
    "lastUpdate",
    "expiryDate",
    "symbolTicker",
    "exchange",
    "segment",
    "scripCode",
    "scripName",
    "scripToken",
    "strikePrice",
    "optionType",
    "underFyToken",
    "underExSymbol",
    "fyersExtra1",
    "fyersExtra2",
)

XNSE_CSV = "XNSE-FYERS.csv"
XNFO_CSV = "XNFO-FYERS.csv"
XNCD_CSV = "XNCD-FYERS.csv"
XBSE_CSV = "XBSE-FYERS.csv"
XBFO_CSV = "XBFO-FYERS.csv"
XMCX_CSV = "XMCX-FYERS.csv"

XNSE_SCRIPT = "XNSE-FYERS.py"
XNFO_SCRIPT = "XNFO-FYERS.py"
XNCD_SCRIPT = "XNCD-FYERS.py"
XBSE_SCRIPT = "XBSE-FYERS.py"
XBFO_SCRIPT = "XBFO-FYERS.py"
XMCX_SCRIPT = "XMCX-FYERS.py"


@dataclass(frozen=True)
class FyersSegment:
    key: str
    exchange_mic: str
    source_file: str
    output_csv: str
    postgres_table: str
    script_name: str
    cash_market: bool


FYERS_SEGMENTS: tuple[FyersSegment, ...] = (
    FyersSegment("xnse", "XNSE", "NSE_CM.csv", XNSE_CSV, "nse_cm", XNSE_SCRIPT, True),
    FyersSegment("xnfo", "XNFO", "NSE_FO.csv", XNFO_CSV, "nse_fo", XNFO_SCRIPT, False),
    FyersSegment("xncd", "XNCD", "NSE_CD.csv", XNCD_CSV, "nse_cd", XNCD_SCRIPT, False),
    FyersSegment("xbse", "XBSE", "BSE_CM.csv", XBSE_CSV, "bse_cm", XBSE_SCRIPT, True),
    FyersSegment("xbfo", "XBFO", "BSE_FO.csv", XBFO_CSV, "bse_fo", XBFO_SCRIPT, False),
    FyersSegment("xmcx", "XMCX", "MCX_COM.csv", XMCX_CSV, "mcx_com", XMCX_SCRIPT, False),
)

_FYERS_BY_KEY = {s.key: s for s in FYERS_SEGMENTS}
_FYERS_BY_CSV = {s.output_csv: s for s in FYERS_SEGMENTS}


def fyers_segment(key: str) -> FyersSegment:
    k = key.strip().lower()
    if k not in _FYERS_BY_KEY:
        raise KeyError(f"unknown Fyers segment {key!r}; want one of {sorted(_FYERS_BY_KEY)}")
    return _FYERS_BY_KEY[k]


def fyers_segment_for_csv(csv_name: str) -> FyersSegment | None:
    return _FYERS_BY_CSV.get(csv_name)


def repo_root() -> Path:
    return _V4_DIR


def helpers_dir() -> Path:
    return _HELPERS_DIR


def secrets_dir() -> Path:
    return _V4_DIR.parent / SECRETS_DIRNAME


def config_ini() -> Path:
    """Live credentials and Postgres URL — ``../secrets/secrets.ini``."""
    return secrets_dir() / SECRETS_INI


def day_dir(*, as_of: date | None = None, root: Path | None = None) -> Path:
    base = root if root is not None else _V4_DIR
    return base / (as_of or date.today()).strftime("%Y%m%d")


def raw_dir(*, as_of: date | None = None, root: Path | None = None) -> Path:
    return day_dir(as_of=as_of, root=root) / RAW_SUBDIR


def normalized_dir(*, as_of: date | None = None, root: Path | None = None) -> Path:
    return day_dir(as_of=as_of, root=root) / NORMALIZED_SUBDIR


def raw_csv(csv_name: str, *, as_of: date | None = None, root: Path | None = None) -> Path:
    return raw_dir(as_of=as_of, root=root) / csv_name


def normalized_csv(csv_name: str, *, as_of: date | None = None, root: Path | None = None) -> Path:
    return normalized_dir(as_of=as_of, root=root) / csv_name


def day_raw_csv(day_dir: Path, csv_name: str) -> Path:
    return day_dir / RAW_SUBDIR / csv_name


def day_normalized_csv(day_dir: Path, csv_name: str) -> Path:
    return day_dir / NORMALIZED_SUBDIR / csv_name


def postgres_schema(date_dir: str) -> str:
    """Postgres schema name for a trading day folder (``v2-YYYYMMDD``)."""
    d = date_dir.strip()
    if len(d) != 8 or not d.isdigit():
        raise ValueError(f"date_dir must be YYYYMMDD, got {date_dir!r}")
    return f"{POSTGRES_SCHEMA_PREFIX}{d}"
