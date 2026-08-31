"""Directory and file path conventions for Premarket v6."""
import configparser
import os
import re
from pathlib import Path
from typing import Optional


def repo_root() -> Path:
    """Find v6-python repo root, honoring PREMARKET_V6_ROOT env var."""
    if env := os.getenv("PREMARKET_V6_ROOT"):
        return Path(env)
    # Walk up from cwd looking for premarketv6 package + pyproject.toml
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "premarketv6" / "__init__.py").exists() and (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not find v6-python repo root. Set PREMARKET_V6_ROOT env var.")


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


def qat_dir() -> Path:
    """Where the check commands write their reports: docs/QAT_GENERATED/.

    Under docs/ and not under data/, because these are the artefact you keep and
    quote -- a data directory gets pruned, and a report that vanished with the
    day it described cannot be cited. Honours PREMARKET_QAT_DIR so a test or a
    CI job can redirect them.
    """
    if env := os.getenv("PREMARKET_QAT_DIR"):
        return Path(env)
    return repo_root() / "docs" / "QAT_GENERATED"


def day_dir(as_of: str) -> Path:
    """Day directory: data/YYYYMMDD/v6/ -- nested under v6/ so this pipeline's
    output never collides with v5-python's data/YYYYMMDD/ tree even though
    both share the same data_root()."""
    return data_root() / as_of / "v6"


def raw_dir(as_of: str) -> Path:
    """Raw data directory for a given day: YYYYMMDD/raw/"""
    return day_dir(as_of) / "raw"


def venue_dir(as_of: str, venue_name: str) -> Path:
    """One venue's own directory for a day: data/YYYYMMDD/{VENUE}/.

    A sibling of the v6/ pipeline tree at the same date, not nested under it,
    and shared by both feeds: Databento venues drop their batch definition
    *.dbn.zst here (see manual_venue_dir), and the Fyers MIC bundles drop their
    raw segment CSVs here (see fyers_segment_path). Both feeds therefore have
    the same shape on disk -- data/YYYYMMDD/XCME/ next to data/YYYYMMDD/XNSE/.
    """
    return data_root() / as_of / venue_name.upper()


def manual_venue_dir(as_of: str, venue_name: str) -> Path:
    """
    A venue's Databento batch definition payload for one day:
    data/YYYYMMDD/{VENUE}/ (e.g. data/20260824/XCBO/) -- a sibling of the v6/
    pipeline tree at the same date, not nested under it.

    Two things land here, indistinguishably to normalize:
      - premarketv6's own --all-symbols download for GLBX.MDP3/OPRA.PILLAR,
        which submits a batch job and downloads the resulting *.dbn.zst here
        directly (sources/databento_src.py's _download_definitions_via_batch)
      - an operator's own manual extraction of a batch job's zip (condition.json/
        metadata.json/manifest.json are ignored; only the *.dbn/*.dbn.zst
        definition file is read), dropped here by hand as an override

    If a file already exists for a venue/day when the automated download would
    run, that's a manual override and normalize/databento_norm.py reads it in
    preference to re-fetching. Nothing here is CSV -- normalize reads the DBN
    directly.
    """
    return data_root() / as_of / venue_name.upper()


def fyers_segment_path(as_of: str, segment: str) -> Path:
    """Raw CSV for one Fyers segment, inside its MIC bundle's venue directory.

    Replaces the flat YYYYMMDD/raw/FYERS/ drop: segments now sit under the MIC
    that owns them (data/YYYYMMDD/XNSE/XNFO-FYERS.csv), which is both the thing
    that gets a counter_token block and the thing normalize emits as one file,
    so the folder matches the unit of work rather than the vendor's name.
    """
    mic = FYERS_SEGMENT_MIC[segment]
    return venue_dir(as_of, mic) / FYERS_RAW_SEGMENTS[segment]


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
    """Raw Databento CSV path: YYYYMMDD/raw/{VENUE}-DATABENTO.csv."""
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


def dated_schema(date_dir: str) -> str:
    """
    Dated snapshot name: v6_YYYYMMDD. Holds the day's contracts and baskets.

    This is v6's own namespace. It was "v4_" while the contracts push targeted
    the Postgres schema Nexus reads -- Nexus's IsV4Schema
    (internal/contract-db/pool/contracts_config.go) tests for that literal
    prefix plus exactly 8 digits. The push is ClickHouse now and Nexus does not
    read ClickHouse, so nothing downstream of this pipeline depends on the old
    name. Reviving a Postgres contracts push means changing Nexus to match, not
    changing this back.
    """
    if not re.match(r"^\d{8}$", date_dir):
        raise ValueError(f"Invalid date_dir format (must be YYYYMMDD): {date_dir}")
    return f"v6_{date_dir}"


def dated_baskets_schema(date_dir: str) -> str:
    """Dated baskets namespace: v6_YYYYMMDD_baskets."""
    return f"{dated_schema(date_dir)}_baskets"


# Static, always-current mirror of the dated snapshot, overwritten every push.
STATIC_SCHEMA = "public"

# Full per-contract fields kept in the flat CSV/SQLite basket exports
# (export.py's aggregate_basket_rows, sqlite_export.py) -- the Postgres
# "baskets" table itself only stores basket + a scripts JSONB array
# (postgres_export.py), since Nexus re-resolves each script itself.
NEXUS_BASKET_COLUMNS = [
    "script", "scriptToken", "scriptInstrumentType2", "optionType",
    "underlying_root", "underlying", "strike", "expiration", "multiplier",
    "currency", "exchange",
]


# Databento `definition` schema passthrough.
#
# The ALL_SYMBOLS path downloads InstrumentDefMsg records, which carry the venue's
# full instrument definition -- tick rules, limit prices, lot sizes, spread legs,
# CFI/security_type, the lot. Only a handful of those fields feed the canonical
# columns above, and the rest used to be discarded at the CSV writer. They are now
# kept verbatim, in both the raw and the normalized CSV.
#
# These are the InstrumentDefMsg attribute names, which are also the raw CSV's
# column names. Deliberately NOT here: instrument_id, raw_symbol and
# instrument_class, which the raw CSV already carries under its own
# MAPPING_COLUMNS names (instrument_id / stype_in_symbol / instrument_class).
DEFINITION_FIELDS = [
    # record + identity
    "ts_recv", "ts_event", "rtype", "publisher_id", "security_update_action",
    "raw_instrument_id", "underlying_id",
    # prices, all 1e-9 fixed point on the wire and kept that way
    "min_price_increment", "display_factor", "high_limit_price", "low_limit_price",
    "max_price_variation", "unit_of_measure_qty", "min_price_increment_amount",
    "price_ratio", "strike_price",
    # lifecycle timestamps, nanoseconds since epoch
    "expiration", "activation",
    # sizes and depth
    "inst_attrib_value", "market_depth_implied", "market_depth", "market_segment_id",
    "max_trade_vol", "min_lot_size", "min_lot_size_block", "min_lot_size_round_lot",
    "min_trade_vol", "contract_multiplier", "decay_quantity", "original_contract_size",
    # calendar
    "appl_id", "maturity_year", "maturity_month", "maturity_day", "maturity_week",
    "decay_start_date", "channel_id",
    # text / classification
    "currency", "settl_currency", "secsubtype", "group", "exchange", "asset", "cfi",
    "security_type", "unit_of_measure", "underlying", "strike_price_currency",
    # single-char enums, written as their one-character code
    "match_algorithm", "user_defined_instrument",
    # display and tick rules
    "main_fraction", "price_display_format", "sub_fraction", "underlying_product",
    "contract_multiplier_unit", "flow_schedule_type", "tick_rule",
    # spread legs
    "leg_count", "leg_index", "leg_instrument_id", "leg_raw_symbol",
    "leg_instrument_class", "leg_side", "leg_price", "leg_delta",
    "leg_ratio_price_numerator", "leg_ratio_price_denominator",
    "leg_ratio_qty_numerator", "leg_ratio_qty_denominator", "leg_underlying_id",
]

# The same fields in the normalized CSV, prefixed. The prefix is not decoration:
# currency, expiration, exchange, underlying and strike_price all collide with a
# canonical column that means something different -- normalized "expiration" is a
# session close in UTC, definition "expiration" is the venue's last eligible trade
# time. Prefixing every field rather than only the four that clash keeps one rule
# instead of a list of exceptions.
DEFINITION_COLUMN_PREFIX = "def_"
DEFINITION_PASSTHROUGH_COLUMNS = [DEFINITION_COLUMN_PREFIX + f for f in DEFINITION_FIELDS]


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
    # Broker symbology (see normalize/broker_script.py). Appended rather than
    # inserted so any consumer reading these CSVs positionally keeps working.
    # Only brokerScript1 is derived today; 2-4 are reserved and always blank.
    "brokerScript1",
    "brokerScript2",
    "brokerScript3",
    "brokerScript4",
    # Per-venue positional counter carrying the venue's two-digit prefix in its
    # leading digits, numbered by normalize/counter_token.assign. scriptToken
    # stays each source's own instrument id, which is only unique within that
    # source; this is the collision-free key the plugin/pg schema pushes as its
    # token. Populated for EVERY venue -- the Databento path assigns it in
    # normalize/databento_norm.py and the Fyers path in normalize/fields.py --
    # so it is never blank. Appended, like the broker columns, so positional
    # readers keep working.
    "counterToken",
    # Stable across days, unlike counterToken above: a script keeps its number
    # for as long as it keeps appearing, and a number is only reused once its
    # script stops appearing. Carried in manifest.json per day. This is the one
    # to join on across dates -- counterToken is positional and must not be.
    "counterTokenV2",
] + DEFINITION_PASSTHROUGH_COLUMNS

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
    # First element is the NORMALIZED output (Parquet); the list is the RAW Fyers
    # CSVs it is built from, which stay CSV because that is what the vendor ships.
    "XNSE": ("XNSE-FYERS.parquet", "xnse", ["XNSE-FYERS.csv", "XNFO-FYERS.csv", "XNCD-FYERS.csv"]),
    "XBOM": ("XBOM-FYERS.parquet", "xbom", ["XBSE-FYERS.csv", "XBFO-FYERS.csv"]),  # BSE -> XBOM MIC
    "XIMC": ("XIMC-FYERS.parquet", "ximc", ["XMCX-FYERS.csv"]),
}

# Segment -> owning MIC bundle, derived from FYERS_MIC_BUNDLES so a segment can
# never be listed in one place and missing from the other.
FYERS_SEGMENT_MIC = {
    segment: mic
    for mic, (_out, _table, sources) in FYERS_MIC_BUNDLES.items()
    for segment, filename in FYERS_RAW_SEGMENTS.items()
    if filename in sources
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


def promote_staging(temp_path, output_path) -> None:
    """Rename a completed staging file onto its real name.

    Windows refuses the rename with WinError 5 while anything holds the target
    open -- Excel takes an exclusive lock on a CSV, so simply having the previous
    day's output open in a spreadsheet aborts the step after all the work is done.
    The staging file is deliberately left in place when that happens: it is
    complete, and re-running only to hit the same lock wastes the whole pass.
    """
    try:
        temp_path.replace(output_path)
    except PermissionError as e:
        raise PermissionError(
            f"cannot replace {output_path.name}: it is open in another program "
            f"(Excel locks CSVs exclusively). Close it and re-run; the finished "
            f"output is already staged at {temp_path.name} -- moving that file over "
            f"{output_path.name} by hand is equivalent. Original error: {e}"
        ) from e
