"""Databento API keys and Postgres URL — read ``../secrets/secrets.ini``."""

from __future__ import annotations

import configparser
import os

from symbology_paths import config_ini

CONFIG_INI = config_ini()


def _flat_ini_keys() -> dict[str, str]:
    """``API_KEY=...`` / ``API_KEY_ES=...`` lines (no section)."""
    if not CONFIG_INI.is_file():
        return {}
    out: dict[str, str] = {}
    for line in CONFIG_INI.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def _section_ini() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if CONFIG_INI.is_file():
        cp.read(CONFIG_INI, encoding="utf-8")
    return cp


def get_api_key() -> str:
    """Key 2 — OPRA.PILLAR, EQUS.MINI (``api_key`` / ``API_KEY``)."""
    k = (os.environ.get("DATABENTO_API_KEY") or "").strip()
    if k:
        return k
    flat = _flat_ini_keys()
    if flat.get("API_KEY"):
        return flat["API_KEY"]
    cp = _section_ini()
    if cp.has_section("databento"):
        key = cp.get("databento", "api_key", fallback="").strip()
        if key:
            return key
    raise ValueError(f"Set API_KEY or [databento] api_key in {CONFIG_INI}")


def get_api_key_es() -> str:
    """Key 1 — GLBX.MDP3 (``api_key_es`` / ``API_KEY_ES``). Falls back to ``get_api_key()``."""
    k = (os.environ.get("DATABENTO_API_KEY_ES") or "").strip()
    if k:
        return k
    flat = _flat_ini_keys()
    if flat.get("API_KEY_ES"):
        return flat["API_KEY_ES"]
    cp = _section_ini()
    if cp.has_section("databento"):
        key_es = cp.get("databento", "api_key_es", fallback="").strip()
        if key_es:
            return key_es
    return get_api_key()


def get_database_url() -> str:
    """Postgres primary (writes) — ``DATABASE_URL`` env or ``[postgres] database_url``."""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return url
    cp = _section_ini()
    if cp.has_section("postgres"):
        u = cp.get("postgres", "database_url", fallback="").strip()
        if u:
            return u
    raise ValueError(
        f"Set DATABASE_URL or [postgres] database_url in {CONFIG_INI} "
        "(primary :7710, not replica :7711)",
    )
