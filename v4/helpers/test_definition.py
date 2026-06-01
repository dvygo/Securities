#!/usr/bin/env python3
"""Probe Databento **definition** schema (``InstrumentDefMsg``) on Live and Historical.

Databento does not expose a separate REST "definition endpoint". Contract reference
data is the ``definition`` **schema** on the same time-series APIs:

- **Live:** ``db.Live().subscribe(..., schema="definition")`` -> ``InstrumentDefMsg``
  (plus ``SymbolMappingMsg`` for symbology after session start).
- **Historical:** ``Historical.timeseries.get_range(..., schema="definition")`` -> same DBN records.

Docs: https://databento.com/docs/schemas-and-data-formats/instrument-definitions

Examples::

  python test_definition.py --mode live --dataset OPRA.PILLAR --symbol AAPL.OPT
  python test_definition.py --mode historical --dataset OPRA.PILLAR --symbol AAPL.OPT
  python test_definition.py --mode both --dataset GLBX.MDP3 --symbol ES.OPT --seconds 15
  python test_definition.py --mode live --dataset EQUS.MINI --symbol AAPL --stype-in raw_symbol

API key: ``conf/config.ini`` (``api_key`` or ``api_key_es`` for GLBX).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import databento as db
from databento.common.error import BentoError

_V4_DIR = Path(__file__).resolve().parent.parent
_HELPERS_DIR = Path(__file__).resolve().parent

# Fields we care about for contract parsing (subset of InstrumentDefMsg).
_DEF_FIELDS = (
    "instrument_id",
    "raw_symbol",
    "instrument_class",
    "security_update_action",
    "expiration",
    "activation",
    "strike_price",
    "strike_price_currency",
    "currency",
    "exchange",
    "underlying",
    "underlying_id",
    "unit_of_measure",
    "unit_of_measure_qty",
    "min_price_increment",
    "contract_multiplier",
    "asset",
    "cfi",
)


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).split(b"\x00", 1)[0].decode("utf-8", errors="replace").rstrip()
    if isinstance(v, str):
        return v.rstrip()
    return str(v).rstrip()


def _ns_to_iso(ns: Any) -> str | None:
    if ns is None:
        return None
    try:
        n = int(ns)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(n / 1e9, tz=timezone.utc).isoformat()


def _instrument_def_row(rec: Any) -> dict[str, Any] | None:
    if type(rec).__name__ != "InstrumentDefMsg":
        return None
    out: dict[str, Any] = {"_rtype": "InstrumentDefMsg"}
    for name in _DEF_FIELDS:
        if not hasattr(rec, name):
            continue
        v = getattr(rec, name)
        if name in ("expiration", "activation"):
            out[name] = _ns_to_iso(v)
            out[f"{name}_ns"] = int(v) if v is not None else None
        elif name in ("strike_price", "min_price_increment", "contract_multiplier"):
            try:
                out[name] = float(v) if v is not None else None
            except (TypeError, ValueError):
                out[name] = v
        elif name in ("instrument_id", "underlying_id"):
            out[name] = int(v) if v is not None else None
        elif isinstance(v, (bytes, bytearray, memoryview)):
            out[name] = _coerce_str(v)
        else:
            out[name] = v
    if hasattr(rec, "pretty_strike_price"):
        try:
            out["pretty_strike_price"] = rec.pretty_strike_price
        except Exception:
            pass
    return out


def _sym_mapping_row(rec: Any) -> dict[str, Any] | None:
    if type(rec).__name__ != "SymbolMappingMsg":
        return None
    return {
        "_rtype": "SymbolMappingMsg",
        "instrument_id": int(getattr(rec, "instrument_id")),
        "stype_in_symbol": _coerce_str(getattr(rec, "stype_in_symbol", "")),
        "stype_out_symbol": _coerce_str(getattr(rec, "stype_out_symbol", "")),
    }


def _iso_to_date(iso: str) -> date:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.date()


def _dataset_definition_range(hist: db.Historical, dataset: str) -> tuple[date, date, str]:
    """Return (first_day, last_day, end_exclusive_iso) for schema=definition."""
    meta = hist.metadata.get_dataset_range(dataset)
    sch = meta.get("schema", {})
    block = sch.get("definition", meta) if isinstance(sch, dict) else meta
    start_iso = block.get("start") or meta.get("start", "")
    end_iso = block.get("end") or meta.get("end", "")
    if not start_iso or not end_iso:
        raise ValueError(f"metadata.get_dataset_range({dataset}) missing start/end")
    first_day = _iso_to_date(start_iso)
    end_day = _iso_to_date(end_iso)
    # API `end` is exclusive (midnight UTC); last requestable session is the prior day.
    last_day = end_day - timedelta(days=1)
    if last_day < first_day:
        last_day = first_day
    return first_day, last_day, end_iso


def _resolve_hist_start(
    hist: db.Historical,
    dataset: str,
    user_start: date | None,
) -> tuple[date, date, date, str]:
    """Pick a session day inside entitlement; default = latest available."""
    first_day, last_day, end_iso = _dataset_definition_range(hist, dataset)
    if user_start is None:
        chosen = last_day
    else:
        chosen = user_start
        if chosen > last_day:
            chosen = last_day
        if chosen < first_day:
            chosen = first_day
    return chosen, first_day, last_day, end_iso


def _api_key_for_dataset(dataset: str) -> str:
    if str(_HELPERS_DIR) not in sys.path:
        sys.path.insert(0, str(_HELPERS_DIR))
    from config import get_api_key, get_api_key_es

    ds = dataset.upper()
    if ds == "GLBX.MDP3":
        return get_api_key_es()
    return get_api_key()


def _default_stype_in(dataset: str, symbol: str) -> str:
    ds = dataset.upper()
    if ds == "GLBX.MDP3":
        return "parent" if "." in symbol else "raw_symbol"
    if ds == "EQUS.MINI":
        return "raw_symbol"
    # OPRA.PILLAR
    return "parent" if symbol.endswith(".OPT") else "raw_symbol"


def _live_definitions(
    key: str,
    *,
    dataset: str,
    symbols: list[str],
    stype_in: str,
    seconds: float,
    max_defs: int,
    max_maps: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    defs: list[dict[str, Any]] = []
    maps: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    client_holder: list[Any] = []

    def on_record(rec: Any) -> None:
        rname = type(rec).__name__
        counts[rname] += 1
        drow = _instrument_def_row(rec)
        if drow is not None and len(defs) < max_defs:
            defs.append(drow)
            return
        mrow = _sym_mapping_row(rec)
        if mrow is not None and len(maps) < max_maps:
            maps.append(mrow)

    client = db.Live(key=key)
    client_holder.append(client)
    client.subscribe(
        dataset=dataset,
        schema="definition",
        symbols=symbols,
        stype_in=stype_in,
        start=0,
    )
    client.add_callback(on_record)

    def _stop() -> None:
        try:
            client.stop()
        except Exception:
            pass

    t = threading.Timer(seconds, _stop)
    t.daemon = True
    t.start()
    try:
        client.start()
        client.block_for_close(timeout=max(60.0, seconds + 45.0))
    except BentoError as exc:
        print(f"Live error: {exc}", file=sys.stderr)
    finally:
        t.cancel()
        try:
            client.terminate()
        except Exception:
            pass
    return defs, maps, counts


def _historical_definitions(
    key: str,
    *,
    dataset: str,
    symbols: list[str],
    stype_in: str,
    start: date,
    max_defs: int,
    hist: db.Historical | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    hist = hist or db.Historical(key)
    try:
        store = hist.timeseries.get_range(
            dataset=dataset,
            schema="definition",
            symbols=symbols,
            stype_in=stype_in,
            start=start,
            end=start + timedelta(days=1),
        )
    except BentoError as exc:
        print(f"Historical error: {exc}", file=sys.stderr)
        return [], Counter()

    counts: Counter[str] = Counter()
    defs: list[dict[str, Any]] = []
    for rec in store:
        rname = type(rec).__name__
        counts[rname] += 1
        row = _instrument_def_row(rec)
        if row is not None and len(defs) < max_defs:
            defs.append(row)
    return defs, counts


def _print_sample(title: str, rows: list[dict[str, Any]], *, n: int = 5) -> None:
    print(f"\n=== {title} (showing {min(n, len(rows))} of {len(rows)}) ===", file=sys.stderr)
    for row in rows[:n]:
        print(json.dumps(row, indent=2, default=str))
    if len(rows) > n:
        print(f"... {len(rows) - n} more", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        choices=("live", "historical", "both"),
        default="live",
        help="live=db.Live, historical=timeseries.get_range, both=compare",
    )
    p.add_argument(
        "--dataset",
        default="OPRA.PILLAR",
        help="e.g. OPRA.PILLAR, GLBX.MDP3, EQUS.MINI",
    )
    p.add_argument(
        "--symbol",
        default="AAPL.OPT",
        help="subscribe symbol (e.g. AAPL.OPT, ES.OPT, AAPL)",
    )
    p.add_argument(
        "--stype-in",
        default="",
        help="empty = dataset default (OPRA/GLBX parent, EQUS raw_symbol)",
    )
    p.add_argument("--seconds", type=float, default=20.0, help="Live listen duration")
    p.add_argument("--max-defs", type=int, default=25, help="max InstrumentDefMsg samples")
    p.add_argument("--max-maps", type=int, default=10, help="max SymbolMappingMsg samples (live)")
    p.add_argument(
        "--hist-start",
        default="",
        metavar="YYYY-MM-DD",
        help="Historical session day (default: latest day in dataset range)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print full result JSON on stdout (summary still on stderr)",
    )
    args = p.parse_args(argv)

    dataset = args.dataset.strip().upper()
    symbol = args.symbol.strip().upper()
    symbols = [symbol]
    stype_in = (args.stype_in or "").strip() or _default_stype_in(dataset, symbol)

    try:
        key = _api_key_for_dataset(dataset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    user_hist_start: date | None = None
    if args.hist_start:
        try:
            user_hist_start = datetime.strptime(args.hist_start, "%Y-%m-%d").date()
        except ValueError:
            print("--hist-start must be YYYY-MM-DD", file=sys.stderr)
            return 1

    hist: db.Historical | None = None
    hist_start = user_hist_start or date.today()
    avail_first = avail_last = hist_start
    avail_end_iso = ""
    if args.mode in ("historical", "both"):
        hist = db.Historical(key)
        hist_start, avail_first, avail_last, avail_end_iso = _resolve_hist_start(
            hist, dataset, user_hist_start
        )
        if user_hist_start and user_hist_start != hist_start:
            print(
                f"note: --hist-start {user_hist_start} clamped to {hist_start} "
                f"(dataset definition range {avail_first} .. {avail_last}, "
                f"end exclusive {avail_end_iso})",
                file=sys.stderr,
            )
        elif user_hist_start is None:
            print(
                f"note: using latest available definition day {hist_start} "
                f"(range {avail_first} .. {avail_last}, end exclusive {avail_end_iso})",
                file=sys.stderr,
            )

    print(
        f"dataset={dataset} symbol={symbol!r} stype_in={stype_in!r} schema=definition",
        file=sys.stderr,
    )

    result: dict[str, Any] = {
        "dataset": dataset,
        "symbol": symbol,
        "stype_in": stype_in,
        "schema": "definition",
    }

    if args.mode in ("live", "both"):
        print(
            f"\n--- Live (~{args.seconds:.0f}s) ---\n"
            "Expect InstrumentDefMsg (contract fields) and SymbolMappingMsg (id<->symbol).",
            file=sys.stderr,
        )
        defs, maps, counts = _live_definitions(
            key,
            dataset=dataset,
            symbols=symbols,
            stype_in=stype_in,
            seconds=args.seconds,
            max_defs=args.max_defs,
            max_maps=args.max_maps,
        )
        print(f"record counts: {dict(counts)}", file=sys.stderr)
        _print_sample("InstrumentDefMsg", defs)
        if maps:
            _print_sample("SymbolMappingMsg", maps, n=3)
        result["live"] = {
            "record_counts": dict(counts),
            "instrument_defs": defs,
            "symbol_mappings": maps,
        }

    if args.mode in ("historical", "both"):
        print(
            f"\n--- Historical (start={hist_start}) ---\n"
            "Same schema via timeseries.get_range - not a separate definition URL.",
            file=sys.stderr,
        )
        defs_h, counts_h = _historical_definitions(
            key,
            dataset=dataset,
            symbols=symbols,
            stype_in=stype_in,
            start=hist_start,
            max_defs=args.max_defs,
            hist=hist,
        )
        print(f"record counts: {dict(counts_h)}", file=sys.stderr)
        _print_sample("InstrumentDefMsg (historical)", defs_h)
        result["historical"] = {
            "start": hist_start.isoformat(),
            "available_first": avail_first.isoformat(),
            "available_last": avail_last.isoformat(),
            "available_end_exclusive": avail_end_iso,
            "record_counts": dict(counts_h),
            "instrument_defs": defs_h,
        }

    if args.json:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")

    hist_defs = (result.get("historical") or {}).get("instrument_defs") or []
    live_defs = (result.get("live") or {}).get("instrument_defs") or []
    if not live_defs and not hist_defs:
        print(
            "\nNo InstrumentDefMsg captured. Try longer --seconds, parent symbol "
            "(e.g. AAPL.OPT), or --mode historical.",
            file=sys.stderr,
        )
        return 2

    print("\nOK - definition schema returns InstrumentDefMsg contract rows.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
