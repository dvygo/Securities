"""Configuration loading from config.ini and environment variables."""
import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
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
    hist_lookback_days: int = 7


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
    max_maps, hist_lookback_days = 100000, 7

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
            hist_lookback_days = section.getint("hist_lookback_days", hist_lookback_days)

    return DatabentoCfg(
        keys=keys,
        live_seconds=live_seconds,
        live_retries=live_retries,
        live_retry_delay_sec=live_retry_delay_sec,
        max_maps=max_maps,
        hist_lookback_days=hist_lookback_days,
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
