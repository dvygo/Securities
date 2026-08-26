"""Configuration loading from config.ini and environment variables."""
import configparser
import os
from dataclasses import dataclass, field
from typing import List, Optional

from . import paths


DATABENTO_EXCHANGES = ("XNAS", "XCBO", "XCME")


@dataclass
class DatabentoCfg:
    """Databento configuration."""
    keys: dict[str, str]  # exchange code (XNAS/XCBO/XCME) -> API key
    live_seconds: int = 10
    live_retries: int = 3
    live_retry_delay_sec: int = 2
    max_maps: int = 100000


EXCHANGE_SECTION_PREFIX = "EXCHANGE:"


@dataclass
class ExchangeCfg:
    """One [EXCHANGE:<CODE>] section: how a venue is downloaded.

    Replaces the venue table that used to be hardcoded in
    sources/databento_src.VENUE_CONFIGS, plus the global
    [databento] hist_lookback_days. Both are gone; this is the only place a
    venue is described.
    """
    venue_name: str                       # MIC as written in the section header
    feed: str = "databento"               # databento | fyers
    # Numbering identity: ONE knob. Both token blocks are derived from it --
    # counterToken gets block (venue_id*2 - 1), counterTokenV2 gets (venue_id*2)
    # -- so two venues can never be configured onto the same block and v1 can
    # never be configured onto v2's. Disjointness is structural here rather than
    # something validate() has to police across three hand-maintained integers.
    # 0 means unset, which is a validation error for a venue that is numbered.
    venue_id: int = 0
    dataset: str = ""                     # Databento dataset id
    schema: str = "definition"
    stype_in: str = "raw_symbol"
    stype_in_all_symbols: str = ""        # blank -> same as stype_in
    all_symbols_default: bool = False
    # Narrowing-only bounds on the definition batch window, "HH:MM" UTC or blank.
    # They can shrink the day but never widen it: the upper bound is still
    # min()'d with the dataset's live available end, which is what stops the
    # 422 data_end_after_available_end that a fixed end always eventually hits.
    batch_start_time: str = ""
    batch_end_time: str = ""
    # How many days before date_dir the definition window opens. 1 = "yesterday
    # 00:00Z through latest available", which is what keeps an early-morning run
    # useful: OPRA does not publish today's snapshot until ~10:00-11:00Z and
    # EQUS until ~05:00-06:00Z, so a window starting at today 00:00Z is simply
    # empty before then. 0 restores the single-day window.
    batch_lookback_days: int = 1
    definition_ready_ratio: float = 0.5
    hist_pin_latest_session: bool = False
    hist_lookback_days: int = 7

    @property
    def counter_base(self) -> int:
        """counterToken's block. 0 when venue_id is unset (venue not numbered)."""
        return self.venue_id * 2 - 1 if self.venue_id else 0

    @property
    def counter_base_v2(self) -> int:
        """counterTokenV2's block, always counter_base + 1."""
        return self.venue_id * 2 if self.venue_id else 0

    @property
    def all_symbols_stype_in(self) -> str:
        return self.stype_in_all_symbols or self.stype_in


def load_exchanges() -> dict[str, ExchangeCfg]:
    """Every [EXCHANGE:<CODE>] section, keyed by lowercased code.

    The section set is authoritative: a venue with no section is unknown to
    the pipeline. There is deliberately no built-in fallback table -- that is
    what made the old VENUE_CONFIGS and config.ini disagree silently.
    """
    config_file = paths.config_ini()
    if not config_file.exists():
        return {}
    cfg = configparser.ConfigParser()
    cfg.read(config_file)

    out: dict[str, ExchangeCfg] = {}
    for name in cfg.sections():
        if not name.startswith(EXCHANGE_SECTION_PREFIX):
            continue
        code = name[len(EXCHANGE_SECTION_PREFIX):].strip().upper()
        if not code:
            continue
        sec = cfg[name]
        out[code.lower()] = ExchangeCfg(
            venue_name=code,
            feed=sec.get("feed", "databento").strip(),
            venue_id=sec.getint("venue_id", 0),
            dataset=sec.get("dataset", "").strip(),
            schema=sec.get("schema", "definition").strip(),
            stype_in=sec.get("stype_in", "raw_symbol").strip(),
            stype_in_all_symbols=sec.get("stype_in_all_symbols", "").strip(),
            all_symbols_default=sec.getboolean("all_symbols_default", False),
            batch_start_time=sec.get("batch_start_time", "").strip(),
            batch_end_time=sec.get("batch_end_time", "").strip(),
            batch_lookback_days=sec.getint("batch_lookback_days", 1),
            definition_ready_ratio=sec.getfloat("definition_ready_ratio", 0.5),
            hist_pin_latest_session=sec.getboolean("hist_pin_latest_session", False),
            hist_lookback_days=sec.getint("hist_lookback_days", 7),
        )
    return out


@dataclass
class FyersCfg:
    """Fyers configuration."""
    base_url: str
    user_agent: str
    timeout_sec: int = 120
    retries: int = 3
    retry_delay_sec: int = 2


@dataclass
class NormalizerCfg:
    """Normalizer configuration (exchange/multiplier overrides)."""
    glbx_underlying: str = "ES"
    glbx_multiplier: int = 100000
    glbx_exchange: str = "XCME"
    opra_exchange: str = "XCBO"
    opra_multiplier: int = 100000
    equs_exchange: str = "XNAS"
    equs_multiplier: int = 1
    xnse_exchange: str = "XNSE"
    xnfo_exchange: str = "XNFO"
    xncd_exchange: str = "XNCD"
    xbse_exchange: str = "XBSE"
    xbfo_exchange: str = "XBFO"
    xmcx_exchange: str = "XIMC"


def load_databento() -> DatabentoCfg:
    """Load Databento config: per-exchange keys from keys.ini, other settings from config.ini.

    Key resolution per exchange (XNAS/XCBO/XCME), highest priority first:
    1. DATABENTO_KEY_<EXCHANGE> env var
    2. keys.ini [<DATABENTO_ENV, default "production">] key_<EXCHANGE>
    """
    env = os.getenv("DATABENTO_ENV", "production")

    keys: dict[str, str] = {exchange: "" for exchange in DATABENTO_EXCHANGES}
    keys_file = paths.keys_ini()
    if keys_file.exists():
        keys_cfg = configparser.ConfigParser()
        keys_cfg.read(keys_file)
        if env in keys_cfg:
            section = keys_cfg[env]
            for exchange in DATABENTO_EXCHANGES:
                keys[exchange] = section.get(f"key_{exchange}", "")

    for exchange in DATABENTO_EXCHANGES:
        if override := os.getenv(f"DATABENTO_KEY_{exchange}"):
            keys[exchange] = override

    live_seconds, live_retries, live_retry_delay_sec = 10, 3, 2
    max_maps = 100000

    config_file = paths.config_ini()
    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "databento" in cfg:
            section = cfg["databento"]
            live_seconds = section.getint("live_seconds", live_seconds)
            live_retries = section.getint("live_retries", live_retries)
            live_retry_delay_sec = section.getint("live_retry_delay_sec", live_retry_delay_sec)
            max_maps = section.getint("max_maps", max_maps)

    return DatabentoCfg(
        keys=keys,
        live_seconds=live_seconds,
        live_retries=live_retries,
        live_retry_delay_sec=live_retry_delay_sec,
        max_maps=max_maps,
    )


def load_fyers() -> FyersCfg:
    """Load Fyers config from config.ini or hardcoded defaults."""
    config_file = paths.config_ini()

    # Default public Fyers URL
    defaults = {
        "base_url": "https://public.fyers.in/sym_details",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "timeout_sec": 120,
        "retries": 3,
        "retry_delay_sec": 2,
    }

    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "fyers" in cfg:
            section = cfg["fyers"]
            return FyersCfg(
                base_url=section.get("base_url", defaults["base_url"]),
                user_agent=section.get("user_agent", defaults["user_agent"]),
                timeout_sec=section.getint("timeout_sec", defaults["timeout_sec"]),
                retries=section.getint("retries", defaults["retries"]),
                retry_delay_sec=section.getint("retry_delay_sec", defaults["retry_delay_sec"]),
            )

    return FyersCfg(**defaults)


def load_normalizer() -> NormalizerCfg:
    """Load normalizer config from config.ini or hardcoded defaults."""
    config_file = paths.config_ini()

    cfg_dict = {
        "glbx_underlying": "ES",
        "glbx_multiplier": 100000,
        "glbx_exchange": "XCME",
        "opra_exchange": "XCBO",
        "opra_multiplier": 100000,
        "equs_exchange": "XNAS",
        "equs_multiplier": 1,
        "xnse_exchange": "XNSE",
        "xnfo_exchange": "XNFO",
        "xncd_exchange": "XNCD",
        "xbse_exchange": "XBSE",
        "xbfo_exchange": "XBFO",
        "xmcx_exchange": "XIMC",
    }

    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "normalizer" in cfg:
            section = cfg["normalizer"]
            for key in cfg_dict:
                if key in section:
                    value = section[key]
                    if key.endswith("_multiplier"):
                        cfg_dict[key] = int(value)
                    else:
                        cfg_dict[key] = value

    return NormalizerCfg(**cfg_dict)


@dataclass
class ClickHouseCfg:
    """[clickhouse] config: the contracts push target.

    `port` is the HTTP port, because clickhouse-connect speaks the HTTP
    interface. tcp_port is carried through unused so the one config section
    still describes the server completely for the native clients that read it.
    """
    host: str = "127.0.0.1"
    port: int = 8123
    tcp_port: int = 9000
    database: str = "default"
    username: str = "default"
    password: str = ""
    secure: bool = False


def load_clickhouse() -> ClickHouseCfg:
    """Load [clickhouse] config from config.ini; CLICKHOUSE_* env vars win.

    Env override exists for the same reason DATABASE_URL does on the Postgres
    side: a scheduled run should be able to point at another server without
    editing a file that is shared with the interactive one.
    """
    defaults = ClickHouseCfg()
    section = {}
    config_file = paths.config_ini()
    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "clickhouse" in cfg:
            section = cfg["clickhouse"]

    def pick(key: str, fallback):
        env = os.getenv(f"CLICKHOUSE_{key.upper()}")
        if env:
            return env
        return section.get(key, fallback) if section else fallback

    def as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    return ClickHouseCfg(
        host=str(pick("host", defaults.host)),
        port=int(pick("port", defaults.port)),
        tcp_port=int(pick("tcp_port", defaults.tcp_port)),
        database=str(pick("database", defaults.database)),
        username=str(pick("username", defaults.username)),
        password=str(pick("password", defaults.password)),
        secure=as_bool(pick("secure", defaults.secure)),
    )


@dataclass
class PostgresPluginCfg:
    """[postgres-plugin] config: the appender's own DSN/schema/table, plus an
    exchange (MIC prefix) allow-list -- empty allow-list means push every
    plugin CSV, not none."""
    database_url: str = ""
    schema: str = ""
    table: str = ""
    exchanges: List[str] = field(default_factory=list)


def load_postgres_plugin() -> PostgresPluginCfg:
    """Load [postgres-plugin] config from config.ini, DATABASE_URL_PLUGIN env for the DSN."""
    database_url = os.getenv("DATABASE_URL_PLUGIN", "")
    schema = ""
    table = ""
    exchanges: List[str] = []

    config_file = paths.config_ini()
    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "postgres-plugin" in cfg:
            section = cfg["postgres-plugin"]
            if not database_url:
                database_url = section.get("database_url", "")
            schema = section.get("schema", "")
            table = section.get("table", "")
            exchanges = [x.strip().upper() for x in section.get("exchanges", "").split(",") if x.strip()]

    return PostgresPluginCfg(database_url=database_url, schema=schema, table=table, exchanges=exchanges)


def database_url(override: Optional[str] = None) -> str:
    """
    Get database URL with precedence:
    1. override argument
    2. DATABASE_URL env var
    3. config.ini [postgres].database_url
    """
    if override:
        return override

    if env_url := os.getenv("DATABASE_URL"):
        return env_url

    config_file = paths.config_ini()
    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "postgres" in cfg and "database_url" in cfg["postgres"]:
            return cfg["postgres"]["database_url"]

    raise ValueError("No database URL found: set --database-url, DATABASE_URL env, or postgres.database_url in config.ini")
