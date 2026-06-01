#!/usr/bin/env python3
"""Enrich v4 symbology CSVs with normalized trading columns.

Reads raw dumps under ``v4/YYYYMMDD/raw/``, writes slim files to ``v4/YYYYMMDD/normalized/``:

- ``raw/XCME-DATABENTO.csv`` → ``normalized/XCME-DATABENTO.csv``
- ``raw/XCBO-DATABENTO.csv`` → stripped by ``strip.py`` first; then ``normalized/XCBO-DATABENTO.csv``
- ``raw/XNAS-DATABENTO.csv`` → ``normalized/XNAS-DATABENTO.csv``
- ``raw/X*-FYERS.csv`` → ``normalized/X*-FYERS.csv`` (six India segments)

  python normalizer.py
  python normalizer.py --date-dir 20260521 --as-of 2026-05-21
  python normalizer.py --dry-run

Normalized output columns: ``date``, ``exchange``, ``underlying_root``, ``underlying``,
``strike``, ``expiration``, ``multiplier``, ``token``, ``symbol``.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from trading_expiry import is_session

from symbology_paths import (
    FYERS_SEGMENTS,
    FyersSegment,
    NORMALIZED_COLUMNS,
    XCBO_CSV,
    XCME_CSV,
    XNAS_CSV,
    config_ini,
    day_normalized_csv,
    day_raw_csv,
    repo_root,
)

_V4_DIR = repo_root()
CONFIG_INI = config_ini()

_EXTRA_COLUMNS = NORMALIZED_COLUMNS[:-2]
_OUTPUT_COLUMNS = NORMALIZED_COLUMNS

_GLBX_CP_STRIKE = re.compile(r"\s+([CP])(\d+(?:\.\d+)?)\s*$", re.I)
_OPRA_OCC_TAIL = re.compile(r"(\d{6})([CP])(\d{8})\s*$", re.I)
_ROOT_WEEKDAY = re.compile(r"^E([1-5])([A-D])$", re.I)
_ROOT_EW = re.compile(r"^EW([1-4])?$", re.I)
_ES_QUARTERLY = re.compile(r"^ES([HMUZ])(\d)", re.I)

_CME_MONTH = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}

_WEEKDAY_LETTER = {"A": 0, "B": 1, "C": 2, "D": 3}
_CME_CALENDAR_MIN = date(2006, 5, 22)


def _load_normalizer_config() -> dict[str, str]:
    defaults = {
        "glbx_underlying": "ES",
        "glbx_multiplier": "100000",
        "glbx_exchange": "XCME",
        "opra_exchange": "XCBO",
        "opra_multiplier": "100000",
        "equs_exchange": "XNAS",
        "equs_multiplier": "1",
        "xnse_exchange": "XNSE",
        "xnfo_exchange": "XNFO",
        "xncd_exchange": "XNCD",
        "xbse_exchange": "XBSE",
        "xbfo_exchange": "XBFO",
        "xmcx_exchange": "XMCX",
    }
    if not CONFIG_INI.is_file():
        return defaults
    cp = configparser.ConfigParser()
    cp.read(CONFIG_INI, encoding="utf-8")
    if not cp.has_section("normalizer"):
        return defaults
    sec = cp["normalizer"]
    for k in defaults:
        if sec.get(k, fallback="").strip():
            defaults[k] = sec.get(k, fallback="").strip()
    return defaults


def _empty_extra() -> dict[str, Any]:
    return {c: "" for c in _EXTRA_COLUMNS}


def _merge_normalized_row(
    row: dict[str, str],
    extra: dict[str, Any],
    *,
    token_key: str = "instrument_id",
    symbol_key: str = "stype_out_symbol",
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for c in _EXTRA_COLUMNS:
        v = extra.get(c, "")
        merged[c] = "" if v is None else v
    merged["token"] = (row.get(token_key) or "").strip()
    merged["symbol"] = (row.get(symbol_key) or "").strip()
    return merged


def _field_set(value: Any) -> bool:
    return value is not None and value != ""


def _missing_strike_expiration(extra: dict[str, Any]) -> bool:
    return not _field_set(extra.get("strike")) and not _field_set(extra.get("expiration"))


def _underlying_root_from_stype_in(stype_in_symbol: str) -> str:
    s = (stype_in_symbol or "").strip().upper()
    if not s:
        return ""
    return s.removesuffix(".OPT").removesuffix(".FUT")


def _glbx_strike_int(stype_out_symbol: str, *, scale: int) -> int | None:
    m = _GLBX_CP_STRIKE.search((stype_out_symbol or "").strip())
    if not m:
        return None
    try:
        return int(round(float(m.group(2)) * scale))
    except ValueError:
        return None


def _next_weekday_on_or_after(as_of: date, target_weekday: int) -> date:
    days = (target_weekday - as_of.weekday()) % 7
    return as_of + timedelta(days=days)


def _cme_adjust_expiry(exp: date) -> date:
    if exp < _CME_CALENDAR_MIN:
        return exp

    d = exp
    for _ in range(8):
        if d < _CME_CALENDAR_MIN:
            break
        try:
            if is_session("CME", d):
                return d
        except Exception:
            break
        d -= timedelta(days=1)
    return exp


def glbx_expiration_yyyymmdd(
    underlying_root: str,
    as_of: date,
    *,
    stype_out_symbol: str = "",
) -> int | None:
    root = (underlying_root or "").strip().upper()
    if not root:
        return None

    m = _ROOT_WEEKDAY.match(root)
    if m:
        letter = m.group(2).upper()
        wd = _WEEKDAY_LETTER.get(letter)
        if wd is None:
            return None
        exp = _cme_adjust_expiry(_next_weekday_on_or_after(as_of, wd))
        return int(exp.strftime("%Y%m%d"))

    if _ROOT_EW.match(root):
        exp = _cme_adjust_expiry(_next_weekday_on_or_after(as_of, 4))
        return int(exp.strftime("%Y%m%d"))

    sym = (stype_out_symbol or "").strip().upper()
    token = sym.split()[0] if sym else ""
    qm = _ES_QUARTERLY.match(token)
    if root == "ES" and qm is None:
        qm = _ES_QUARTERLY.match(sym.replace(" ", ""))
    if qm:
            month = _CME_MONTH.get(qm.group(1).upper())
            yi = int(qm.group(2))
            year = 2000 + yi if yi < 70 else 1900 + yi
            if month:
                # Third Friday of expiry month (standard index futures options anchor).
                d = date(year, month, 1)
                while d.weekday() != 4:
                    d += timedelta(days=1)
                while d.month == month:
                    d += timedelta(days=7)
                d -= timedelta(days=7)
                exp = _cme_adjust_expiry(d)
                return int(exp.strftime("%Y%m%d"))
    return None


def normalize_glbx_row(
    row: dict[str, str],
    as_of: date,
    cfg: dict[str, str],
) -> dict[str, Any]:
    scale = int(cfg.get("glbx_multiplier", "100000") or "100000")
    out = _empty_extra()
    out["date"] = int(as_of.strftime("%Y%m%d"))
    out["exchange"] = cfg.get("glbx_exchange", "XCME")
    st_in = row.get("stype_in_symbol", "")
    st_out = row.get("stype_out_symbol", "")
    root = _underlying_root_from_stype_in(st_in)
    out["underlying_root"] = root
    out["underlying"] = cfg.get("glbx_underlying", "ES")
    strike = _glbx_strike_int(st_out, scale=scale)
    if strike is not None:
        out["strike"] = strike
    exp = glbx_expiration_yyyymmdd(root, as_of, stype_out_symbol=st_out)
    if exp is not None:
        out["expiration"] = exp
    out["multiplier"] = scale
    return out


def _yymmdd_to_yyyymmdd_int(yymmdd: str) -> int | None:
    try:
        yi = int(yymmdd[:2])
        year = 2000 + yi if yi < 70 else 1900 + yi
        return int(f"{year:04d}{yymmdd[2:4]}{yymmdd[4:6]}")
    except (ValueError, TypeError):
        return None


def _parse_opra_occ(symbol: str) -> tuple[str, int | None, int | None]:
    """Return underlying, expiration, OCC strike as thousandths (8-digit int)."""
    s = (symbol or "").strip()
    m = _OPRA_OCC_TAIL.search(s)
    if not m:
        return "", None, None
    yymmdd, _cp, strike8 = m.group(1), m.group(2), m.group(3)
    prefix = s[: m.start()]
    und = re.sub(r"\s+", "", prefix).upper() or prefix.strip().upper()
    exp = _yymmdd_to_yyyymmdd_int(yymmdd)
    try:
        strike_thousandths = int(strike8)
    except ValueError:
        strike_thousandths = None
    return und, exp, strike_thousandths


def normalize_opra_row(
    row: dict[str, str],
    as_of: date,
    cfg: dict[str, str],
) -> dict[str, Any]:
    mult = int(cfg.get("opra_multiplier", "100000") or "100000")
    out = _empty_extra()
    out["date"] = int(as_of.strftime("%Y%m%d"))
    out["exchange"] = cfg.get("opra_exchange", "XCBO")
    root = _underlying_root_from_stype_in(row.get("stype_in_symbol", ""))
    out["underlying_root"] = root
    und, exp, strike_thousandths = _parse_opra_occ(row.get("stype_out_symbol", ""))
    out["underlying"] = und or root.removesuffix(".OPT") or root
    if strike_thousandths is not None:
        # human_price = strike_thousandths / 1000 (OCC dollars); strike = human_price * multiplier
        out["strike"] = strike_thousandths * mult // 1000
    if exp is not None:
        out["expiration"] = exp
    out["multiplier"] = mult
    return out


def normalize_equs_row(
    row: dict[str, str],
    as_of: date,
    cfg: dict[str, str],
) -> dict[str, Any]:
    mult = int(cfg.get("equs_multiplier", "1") or "1")
    out = _empty_extra()
    out["date"] = int(as_of.strftime("%Y%m%d"))
    out["exchange"] = cfg.get("equs_exchange", "XNAS")
    sym = (row.get("stype_out_symbol") or row.get("stype_in_symbol") or "").strip().upper()
    out["underlying_root"] = sym
    out["underlying"] = sym
    out["strike"] = 0
    out["expiration"] = 0
    out["multiplier"] = mult
    return out


def _fyers_expiry_int(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s or s in ("0", "-1"):
        return None
    if len(s) == 8 and s.isdigit():
        return int(s)
    try:
        ts = int(float(s))
        if ts > 1_000_000_000_000:
            ts //= 1000
        if ts > 0:
            return int(datetime.utcfromtimestamp(ts).strftime("%Y%m%d"))
    except (ValueError, OSError, OverflowError):
        pass
    return None


def _fyers_underlying(row: dict[str, str]) -> tuple[str, str]:
    name = (row.get("scripName") or "").strip().upper()
    if name:
        return name, name
    ticker = (row.get("symbolTicker") or "").strip().upper()
    sym = (row.get("symbol") or "").strip()
    if ticker:
        if ":" in ticker:
            ticker = ticker.split(":", 1)[1]
        base = ticker.split("-", 1)[0]
        return base, base
    if ":" in sym:
        tail = sym.split(":", 1)[1].strip().upper()
        base = tail.split("-", 1)[0]
        return base, base
    return sym.upper(), sym.upper()


def _fyers_strike_paise(row: dict[str, str]) -> int | None:
    raw = (row.get("strikePrice") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        if v <= 0:
            return None
        return int(round(v * 100))
    except ValueError:
        return None


def _fyers_lot_size(row: dict[str, str], *, cash: bool) -> int:
    if cash:
        return 1
    raw = (row.get("lotSize") or "").strip()
    try:
        n = int(float(raw))
        return n if n > 0 else 1
    except ValueError:
        return 1


def normalize_fyers_row(
    row: dict[str, str],
    as_of: date,
    cfg: dict[str, str],
    segment: FyersSegment,
) -> dict[str, Any]:
    out = _empty_extra()
    out["date"] = int(as_of.strftime("%Y%m%d"))
    ex_key = f"{segment.key}_exchange"
    out["exchange"] = cfg.get(ex_key, segment.exchange_mic)
    root, und = _fyers_underlying(row)
    out["underlying_root"] = root
    out["underlying"] = und
    mult = _fyers_lot_size(row, cash=segment.cash_market)
    out["multiplier"] = mult
    if segment.cash_market:
        out["strike"] = 0
        out["expiration"] = 0
    else:
        strike = _fyers_strike_paise(row)
        if strike is not None:
            out["strike"] = strike
        exp = _fyers_expiry_int(row.get("expiryDate", ""))
        if exp is not None:
            out["expiration"] = exp
    return out


def _merge_fyers_row(row: dict[str, str], extra: dict[str, Any]) -> dict[str, Any]:
    return _merge_normalized_row(
        row,
        extra,
        token_key="scripCode",
        symbol_key="symbolTicker",
    )


def _rewrite_fyers_csv(
    path: Path,
    segment: FyersSegment,
    *,
    as_of: date,
    cfg: dict[str, str],
    dry_run: bool,
    dest: Path | None = None,
) -> tuple[int, int]:
    out_path = dest if dest is not None else path
    if not path.is_file():
        print(f"skip (missing): {path}", file=sys.stderr)
        return 0, 0

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            print(f"skip (no header): {path}", file=sys.stderr)
            return 0, 0
        rows_in = list(reader)

    warnings = 0
    rows_out: list[dict[str, Any]] = []
    for row in rows_in:
        extra = normalize_fyers_row(row, as_of, cfg, segment)
        if _missing_strike_expiration(extra) and (row.get("symbol") or "").strip():
            warnings += 1
        rows_out.append(_merge_fyers_row(row, extra))

    if dry_run:
        print(f"dry-run: would write {len(rows_out)} rows -> {out_path}", file=sys.stderr)
        return len(rows_in), warnings

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".csv",
        dir=str(out_path.parent),
        prefix=f".{out_path.stem}_",
    )
    try:
        with open(fd, "w", newline="", encoding="utf-8-sig") as fout:
            writer = csv.DictWriter(fout, fieldnames=_OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for r in rows_out:
                writer.writerow(r)
        Path(tmp_name).replace(out_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    msg = f"normalized {len(rows_out)} rows -> {out_path}"
    if warnings:
        msg += f" ({warnings} rows missing strike/expiration)"
    print(msg, file=sys.stderr)
    return len(rows_in), warnings


def normalize_fyers(day_dir: Path, *, as_of: date, cfg: dict[str, str], dry_run: bool) -> None:
    for segment in FYERS_SEGMENTS:
        _rewrite_fyers_csv(
            day_raw_csv(day_dir, segment.output_csv),
            segment,
            as_of=as_of,
            cfg=cfg,
            dry_run=dry_run,
            dest=day_normalized_csv(day_dir, segment.output_csv),
        )


def _merge_row(
    row: dict[str, str],
    extra: dict[str, Any],
) -> dict[str, Any]:
    return _merge_normalized_row(row, extra)


def _rewrite_csv(
    path: Path,
    normalize_fn: Callable[[dict[str, str], date, dict[str, str]], dict[str, Any]],
    *,
    as_of: date,
    cfg: dict[str, str],
    dry_run: bool,
    dest: Path | None = None,
) -> tuple[int, int]:
    out_path = dest if dest is not None else path
    if not path.is_file():
        print(f"skip (missing): {path}", file=sys.stderr)
        return 0, 0

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            print(f"skip (no header): {path}", file=sys.stderr)
            return 0, 0
        rows_in = list(reader)

    warnings = 0
    scale_warnings = 0
    rows_out: list[dict[str, Any]] = []
    for row in rows_in:
        extra = normalize_fn(row, as_of, cfg)
        if _missing_strike_expiration(extra):
            if (row.get("stype_out_symbol") or "").strip():
                warnings += 1
        strike_v = extra.get("strike")
        mult_v = extra.get("multiplier")
        sym_out = (row.get("stype_out_symbol") or "").strip()
        if strike_v not in ("", None) and mult_v not in ("", None) and sym_out:
            try:
                si, mi = int(strike_v), int(mult_v)
                m_occ = _OPRA_OCC_TAIL.search(sym_out)
                if m_occ and mi > 0:
                    t = int(m_occ.group(3))
                    if t % 1000 == 0 and si != t * mi // 1000:
                        scale_warnings += 1
            except (TypeError, ValueError):
                pass
        rows_out.append(_merge_row(row, extra))

    if dry_run:
        print(f"dry-run: would write {len(rows_out)} rows -> {out_path}", file=sys.stderr)
        return len(rows_in), warnings

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".csv",
        dir=str(out_path.parent),
        prefix=f".{out_path.stem}_",
    )
    try:
        with open(fd, "w", newline="", encoding="utf-8-sig") as fout:
            writer = csv.DictWriter(fout, fieldnames=_OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for r in rows_out:
                writer.writerow(r)
        Path(tmp_name).replace(out_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    msg = f"normalized {len(rows_out)} rows -> {out_path}"
    if warnings:
        msg += f" ({warnings} rows missing strike/expiration)"
    if scale_warnings:
        msg += f" ({scale_warnings} rows strike not divisible by multiplier)"
    print(msg, file=sys.stderr)
    return len(rows_in), warnings


def normalize_xcme(day_dir: Path, *, as_of: date, cfg: dict[str, str], dry_run: bool) -> None:
    _rewrite_csv(
        day_raw_csv(day_dir, XCME_CSV),
        normalize_glbx_row,
        as_of=as_of,
        cfg=cfg,
        dry_run=dry_run,
        dest=day_normalized_csv(day_dir, XCME_CSV),
    )


def normalize_xcbo(day_dir: Path, *, as_of: date, cfg: dict[str, str], dry_run: bool) -> None:
    _rewrite_csv(
        day_normalized_csv(day_dir, XCBO_CSV),
        normalize_opra_row,
        as_of=as_of,
        cfg=cfg,
        dry_run=dry_run,
    )


def normalize_xnas(day_dir: Path, *, as_of: date, cfg: dict[str, str], dry_run: bool) -> None:
    _rewrite_csv(
        day_raw_csv(day_dir, XNAS_CSV),
        normalize_equs_row,
        as_of=as_of,
        cfg=cfg,
        dry_run=dry_run,
        dest=day_normalized_csv(day_dir, XNAS_CSV),
    )


def _self_test() -> None:
    as_of = date(2026, 5, 20)
    cfg = _load_normalizer_config()
    row = {
        "instrument_id": "42954304",
        "stype_in_symbol": "E1A.OPT",
        "stype_out_symbol": "E1AM6 C7525",
        "stype_in": "255",
        "stype_out": "255",
        "start_ts": "18446744073709551615",
        "end_ts": "18446744073709551615",
    }
    ex = normalize_glbx_row(row, as_of, cfg)
    assert ex["underlying_root"] == "E1A"
    assert ex["underlying"] == "ES"
    assert ex["exchange"] == "XCME"
    assert ex["strike"] == 752500000
    expected_exp = int(
        _cme_adjust_expiry(_next_weekday_on_or_after(as_of, 0)).strftime("%Y%m%d")
    )
    assert ex["expiration"] == expected_exp
    assert ex["multiplier"] == 100000
    opra_row = {
        "instrument_id": "1",
        "stype_in_symbol": "NVDA.OPT",
        "stype_out_symbol": "NVDA  260522P00110000",
        "stype_in": "255",
        "stype_out": "255",
        "start_ts": "0",
        "end_ts": "0",
    }
    ox = normalize_opra_row(opra_row, as_of, cfg)
    assert ox["strike"] == 11000000
    assert ox["strike"] % ox["multiplier"] == 0
    assert ox["multiplier"] == 100000
    assert ox["expiration"] == 20260522
    frac_row = {**opra_row, "stype_out_symbol": "NVDA  260529P00222500"}
    fx = normalize_opra_row(frac_row, as_of, cfg)
    assert fx["strike"] == 22250000
    fyers_row = {
        "symbol": "NSE:NIFTY25MAY24000CE",
        "symbolTicker": "NIFTY",
        "scripName": "NIFTY",
        "scripToken": "12345",
        "strikePrice": "24000",
        "expiryDate": "1748198400",
        "lotSize": "25",
    }
    seg = next(s for s in FYERS_SEGMENTS if s.key == "xnfo")
    fy = normalize_fyers_row(fyers_row, as_of, cfg, seg)
    assert fy["exchange"] == "XNFO"
    assert fy["strike"] == 2400000
    assert fy["multiplier"] == 25
    cm = normalize_fyers_row(
        {"symbol": "NSE:SBIN-EQ", "symbolTicker": "SBIN", "scripToken": "1", "lotSize": "1"},
        as_of,
        cfg,
        next(s for s in FYERS_SEGMENTS if s.key == "xnse"),
    )
    assert cm["strike"] == 0
    assert cm["expiration"] == 0
    merged = _merge_fyers_row(
        {
            "symbolTicker": "NSE:NIFTY25MAY24000CE",
            "scripCode": "35000",
            "symbol": "NIFTY 25 May 24000 CE",
            "strikePrice": "24000",
            "expiryDate": "1748198400",
            "lotSize": "25",
            "scripName": "NIFTY",
        },
        fy,
    )
    assert merged["token"] == "35000"
    assert merged["symbol"] == "NSE:NIFTY25MAY24000CE"
    glbx_merged = _merge_row(row, ex)
    assert glbx_merged["token"] == "42954304"
    assert glbx_merged["symbol"] == "E1AM6 C7525"
    print("self-test: glbx + OPRA + Fyers OK", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date-dir",
        default="",
        help="YYYYMMDD folder under v4 (default: today)",
    )
    p.add_argument(
        "--as-of",
        default="",
        metavar="YYYY-MM-DD",
        help="Reference date for date/expiration columns (default: today)",
    )
    p.add_argument("--dry-run", action="store_true", help="do not write files")
    p.add_argument("--self-test", action="store_true", help="run built-in checks and exit")
    args = p.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0

    if args.as_of:
        try:
            as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
        except ValueError:
            print("--as-of must be YYYY-MM-DD", file=sys.stderr)
            return 1
    else:
        as_of = date.today()

    day_name = (args.date_dir or "").strip() or as_of.strftime("%Y%m%d")
    day_dir = _V4_DIR / day_name
    if not day_dir.is_dir() and not args.dry_run:
        print(f"warning: {day_dir} does not exist yet", file=sys.stderr)

    cfg = _load_normalizer_config()
    print(f"normalizer: as_of={as_of} dir={day_dir}", file=sys.stderr)
    normalize_xcme(day_dir, as_of=as_of, cfg=cfg, dry_run=args.dry_run)
    normalize_xcbo(day_dir, as_of=as_of, cfg=cfg, dry_run=args.dry_run)
    normalize_xnas(day_dir, as_of=as_of, cfg=cfg, dry_run=args.dry_run)
    normalize_fyers(day_dir, as_of=as_of, cfg=cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
