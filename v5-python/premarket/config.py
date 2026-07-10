"""Configuration loading from config.ini and environment variables."""
import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths


@dataclass
class DatabentoCfg:
    """Databento configuration."""
    api_key: str
    api_key_es: str
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
    """Load Databento config from env or config.ini, with fallback defaults."""
    api_key = os.getenv("DATABENTO_API_KEY")
    api_key_es = os.getenv("DATABENTO_API_KEY_ES")

    config_file = paths.config_ini()
    if config_file.exists():
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if "databento" in cfg:
            section = cfg["databento"]
            if not api_key:
                api_key = section.get("api_key")
            if not api_key_es:
                api_key_es = section.get("api_key_es")
            return DatabentoCfg(
                api_key=api_key or "",
                api_key_es=api_key_es or "",
                live_seconds=section.getint("live_seconds", 10),
                live_retries=section.getint("live_retries", 3),
                live_retry_delay_sec=section.getint("live_retry_delay_sec", 2),
                max_maps=section.getint("max_maps", 100000),
                hist_lookback_days=section.getint("hist_lookback_days", 7),
            )

    return DatabentoCfg(
        api_key=api_key or "",
        api_key_es=api_key_es or "",
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
