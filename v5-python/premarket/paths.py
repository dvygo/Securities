"""Directory and file path conventions for Premarket v5."""
import configparser
import os
import re
from pathlib import Path
from typing import Dict, List, Optional


def repo_root() -> Path:
    """Find v5-python repo root, honoring PREMARKET_V5_ROOT env var."""
    if env := os.getenv("PREMARKET_V5_ROOT"):
        return Path(env)
    # Walk up from cwd looking for premarket package + pyproject.toml
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "premarket" / "__init__.py").exists() and (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not find v5-python repo root. Set PREMARKET_V5_ROOT env var.")


def config_ini() -> Path:
    """Path to config.ini, honoring PREMARKET_CONFIG env var."""
    if env := os.getenv("PREMARKET_CONFIG"):
        return Path(env)
    return repo_root() / "conf" / "config.ini"


def keys_ini() -> Path:
    """Path to conf/keys.ini (Databento per-exchange keys), honoring PREMARKET_KEYS env var."""
    if env := os.getenv("PREMARKET_KEYS"):
        return Path(env)
    return repo_root() / "conf" / "keys.ini"


def secrets_ini() -> Path:
    """Path to secrets/secrets.ini, honoring PREMARKET_SECRETS env var."""
    if env := os.getenv("PREMARKET_SECRETS"):
        return Path(env)
    return repo_root().parent / "secrets" / "secrets.ini"


def _paths_section() -> Optional[configparser.SectionProxy]:
    """The [paths] section of config.ini, if the file and section exist.

    Read directly via configparser rather than the config module, which
    itself imports paths -- avoids a circular import.
    """
    cfg_file = config_ini()
    if not cfg_file.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(cfg_file)
    return cfg["paths"] if "paths" in cfg else None


def _configured_dir(env_var: str, config_key: str, default: Path) -> Path:
    """Resolve a directory: env var, then config.ini's [paths].<config_key>
    (relative paths resolve against repo_root()), then default."""
    if env := os.getenv(env_var):
        return Path(env)
    section = _paths_section()
    if section and config_key in section:
        value = Path(section[config_key])
        return value if value.is_absolute() else repo_root() / value
    return default


def data_root() -> Path:
    """Data root (dated dirs): PREMARKET_DATA_ROOT env, config.ini's
    [paths].data_dir, else ../data."""
    return _configured_dir("PREMARKET_DATA_ROOT", "data_dir", repo_root().parent / "data")


def day_dir(as_of: str) -> Path:
    """Day directory: data/YYYYMMDD/"""
    return data_root() / as_of


def raw_dir(as_of: str) -> Path:
    """Raw data directory for a given day: YYYYMMDD/raw/"""
    return day_dir(as_of) / "raw"


def fyers_raw_dir(as_of: str) -> Path:
    """Fyers raw directory: YYYYMMDD/raw/FYERS/"""
    return raw_dir(as_of) / "FYERS"


def nse_exchange_raw_dir(as_of: str) -> Path:
    """NSE exchange raw directory: YYYYMMDD/raw/NSE_EXCHANGE/NEW FILE FORMAT/"""
    return raw_dir(as_of) / "NSE_EXCHANGE" / "NEW FILE FORMAT"


def normalized_dir(as_of: str) -> Path:
    """Normalized data directory: YYYYMMDD/normalized/"""
    return day_dir(as_of) / "normalized"


def plugin_dir(as_of: str) -> Path:
    """Plugin-format data directory: YYYYMMDD/plugin/ (sibling of normalized/)."""
    return day_dir(as_of) / "plugin"


def databento_raw_csv(as_of: str, venue: str) -> Path:
    """Raw Databento CSV path: YYYYMMDD/raw/{VENUE}-DATABENTO.csv (matches v4-golang naming)."""
    return raw_dir(as_of) / f"{venue.upper()}-DATABENTO.csv"


def baskets_dir() -> Path:
    """Baskets directory: PREMARKET_BASKETS_DIR env, config.ini's
    [paths].baskets_dir, else constituents/baskets/"""
    return _configured_dir("PREMARKET_BASKETS_DIR", "baskets_dir", repo_root() / "constituents" / "baskets")


def contracts_dir() -> Path:
    """Contracts directory: PREMARKET_CONTRACTS_DIR env, config.ini's
    [paths].contracts_dir, else constituents/contracts/"""
    return _configured_dir("PREMARKET_CONTRACTS_DIR", "contracts_dir", repo_root() / "constituents" / "contracts")


def contracts_day_dir(as_of: str) -> Path:
    """Contracts for a day: constituents/contracts/YYYYMMDD/"""
    return contracts_dir() / as_of


def bin_dir() -> Path:
    """Binary/output directory: PREMARKET_BIN_DIR env, config.ini's
    [paths].bin_dir, else bin/"""
    return _configured_dir("PREMARKET_BIN_DIR", "bin_dir", repo_root() / "bin")


def logs_dir() -> Path:
    """Logs directory: PREMARKET_LOGS_DIR env, config.ini's [paths].logs_dir,
    else bin/LOGS/"""
    return _configured_dir("PREMARKET_LOGS_DIR", "logs_dir", bin_dir() / "LOGS")


def ensure_bin_dirs() -> None:
    """Ensure bin/ and bin/LOGS/ directories exist."""
    bin_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)


def postgres_schema(date_dir: str) -> str:
    """
    Dated schema name: v4_YYYYMMDD. Holds a single "contracts" table (all
    exchanges) and a convenience merged "baskets" table. Must stay exactly
    "v4_" + YYYYMMDD (no extra suffix) -- Nexus's own IsV4Schema/
    FormatContractsSchema (internal/contract-db/pool/contracts_config.go)
    hard-require this shape and derive the baskets schema from it.
    """
    if not re.match(r"^\d{8}$", date_dir):
        raise ValueError(f"Invalid date_dir format (must be YYYYMMDD): {date_dir}")
    return f"v4_{date_dir}"


def postgres_baskets_schema(date_dir: str) -> str:
    """
    Dated Nexus-compatible baskets schema: v4_YYYYMMDD_baskets, holding a
    single "baskets" table (one row per basket name, scripts as a JSONB
    array). Fixed by Nexus's FormatBasketsSchema (contractsSchema +
    "_baskets") and internal/contract-db/baskets/load.go, which resolves
    each script against its own contracts index.
    """
    return f"{postgres_schema(date_dir)}_baskets"


# Static, always-current mirror of the dated schema (single "contracts" and
# "baskets" tables), overwritten on every push. Postgres's built-in default
# schema -- Nexus never reads it, so its shape is ours to decide freely.
POSTGRES_STATIC_SCHEMA = "public"

# Full per-contract fields kept in the flat CSV/SQLite basket exports
# (export.py's aggregate_basket_rows, sqlite_export.py) -- the Postgres
# "baskets" table itself only stores basket + a scripts JSONB array
# (postgres_export.py), since Nexus re-resolves each script itself.
NEXUS_BASKET_COLUMNS = [
    "script", "scriptToken", "scriptInstrumentType2", "optionType",
    "underlying_root", "underlying", "strike", "expiration", "multiplier",
    "currency", "exchange",
]


# Canonical normalized column schema (16 columns)
NORMALIZED_COLUMNS = [
    "scriptDetails",
    "scriptInstrumentType",
    "scriptInstrumentType2",
    "multiplier",
    "lotSize",
    "tickSize",
    "ISIN",
    "tradingSessionUTC",
    "expiration",
    "script",
    "scriptToken",
    "underlying_root",
    "underlying",
    "strike",
    "optionType",
    "currency",
]

# Contract columns = date + exchange + normalized columns
CONTRACT_COLUMNS = ["date", "exchange"] + NORMALIZED_COLUMNS

# Fyers raw segments: segment name -> CSV filename (without day directory)
FYERS_RAW_SEGMENTS = {
    "xnse": "XNSE-FYERS.csv",
    "xnfo": "XNFO-FYERS.csv",
    "xncd": "XNCD-FYERS.csv",
    "xbse": "XBSE-FYERS.csv",
    "xbfo": "XBFO-FYERS.csv",
    "xmcx": "XMCX-FYERS.csv",
}

# Fyers MIC bundles: MIC -> (output_csv_name, postgres_table, source_csv_list)
FYERS_MIC_BUNDLES = {
    "XNSE": ("XNSE-FYERS.csv", "xnse", ["XNSE-FYERS.csv", "XNFO-FYERS.csv", "XNCD-FYERS.csv"]),
    "XBOM": ("XBOM-FYERS.csv", "xbom", ["XBSE-FYERS.csv", "XBFO-FYERS.csv"]),  # BSE -> XBOM MIC
    "XIMC": ("XIMC-FYERS.csv", "ximc", ["XMCX-FYERS.csv"]),
}

# NSE segments
NSE_SEGMENTS = {
    "nse_cm": "NSE_CM_Instruments.csv",
    "nse_fo": "NSE_FO_Instruments.csv",
    "nse_cd": "NSE_CD_Instruments.csv",
}

# Basket names, standardized to {MIC}_{purpose} (matches the definition CSV
# filenames under constituents/baskets/ 1:1, except ALL_INDEX_FUTURES, which
# has no file of its own -- it's the union of the three *_INDEX_FUTURES/
# *_FUTURES baskets below). XNAS/XCBO/XCME are venue underlying-symbol lists
# consumed directly by sources/databento_src.py for download selection, not
# basket-refresh targets -- excluded here.
BASKET_NAMES = [
    "XNSE_NIFTYFNO_EQUITY",
    "XNSE_NIFTYFNO_FUTURES_ALL",
    "XNSE_NIFTYFNO_FUTURES_NEAR",
    "ALL_INDEX_FUTURES",
    "XNSE_INDEX_FUTURES_ALL",
    "XNSE_INDEX_FUTURES_NEAR",
    "XBOM_INDEX_FUTURES",
    "XIMC_FUTURES_ALL",
    "XIMC_CRUDE_NEAREST_NXTNEAREST",
    "XIMC_BULLDEX_NEAREST_NXTNEAREST",
    "XNSE_NIFTY50_EQUITY",
    "XNSE_NIFTY100_EQUITY",
    "XNSE_NIFTY200_EQUITY",
    "XNSE_NIFTY500_EQUITY",
    "XNSE_NIFTY500_FUTURES",
]
