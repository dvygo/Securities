#!/usr/bin/env python3
"""OPRA.PILLAR ``db.Live()``: ``SymbolMappingMsg`` (``stype_in_symbol`` → ``stype_out_symbol``, ``instrument_id``). Key: ``__________v3/config.ini`` ``api_key`` (or ``DATABENTO_API_KEY``).

One OCC line (positional): subscribe with ``stype_in=parent`` by default, stop on first mapping.

  python databento_symbology_resolve_test.py "AAPL  260501P00265000"

No positional: load tickers from ``underlyings.csv`` (same dir as this script), append ``.OPT``, subscribe with ``stype_in=parent``. Override list via ``--symbols-file``. Each parent gets its own Live session; rows append to ``YYYYMMDD/opra_pillar_unstripped.csv``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import databento as db
from databento.common.error import BentoError

DATASET = "OPRA.PILLAR"
DEFAULT_UNDERLYINGS_CSV = Path(__file__).resolve().parent / "underlyings.csv"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _default_csv_out(filename: str) -> Path:
    return _repo_root() / date.today().strftime("%Y%m%d") / filename


def _read_symbols(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


def _append_symbol_mapping_csv(
    csv_p: Path,
    rows: list[dict[str, Any]],
    csv_fields: tuple[str, ...],
) -> None:
    """Append rows; write header only if the file is missing or empty."""
    expected = list(csv_fields)
    if csv_p.exists() and csv_p.stat().st_size > 0:
        with csv_p.open(encoding="utf-8-sig", newline="") as rf:
            first = next(csv.reader(rf), None)
        if first != expected:
            print(
                f"error: {csv_p} has incompatible header (got {len(first or [])} cols, "
                f"expected {len(expected)}). Delete the file or run on a fresh day folder "
                f"before appending Live symbology.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    has_body = csv_p.exists() and csv_p.stat().st_size > 0
    with csv_p.open("a", encoding="utf-8-sig", newline="") as cf:
        w = csv.DictWriter(cf, fieldnames=csv_fields, extrasaction="ignore")
        if not has_body:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in csv_fields})


def _parent(s: str) -> str:
    return s if s.endswith(".OPT") else f"{s}.OPT"


def _norm_sym(s: str) -> str:
    return " ".join(s.split()).strip().upper()


def _occ_key(s: str) -> str:
    """Compare OCC strings ignoring all whitespace (feed vs human spacing)."""
    return "".join(s.split()).upper()


def _occ_opra_padded(user: str) -> str:
    """OPRA ``raw_symbol`` for Live: root left-padded to 6 chars + YYMMDD + C/P + 8-digit strike (OSI)."""
    compact = "".join(user.split()).upper()
    if len(compact) < 15 or compact[-9] not in "CP":
        return _norm_sym(user)
    strike = compact[-8:]
    if not strike.isdigit():
        return _norm_sym(user)
    date = compact[-15:-9]
    if not date.isdigit() or len(date) != 6:
        return _norm_sym(user)
    root = compact[:-15]
    if not root or len(root) > 6:
        return _norm_sym(user)
    return root[:6].ljust(6) + date + compact[-9] + strike


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).split(b"\x00", 1)[0].decode("utf-8", errors="replace").rstrip()
    if isinstance(v, str):
        return v.rstrip()
    return str(v).rstrip()


def _api_key() -> str:
    v3 = _repo_root()
    if str(v3) not in sys.path:
        sys.path.insert(0, str(v3))
    from v3_config import get_api_key

    return get_api_key()


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if hasattr(obj, "as_dict"):
        return _to_jsonable(obj.as_dict())
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return _to_jsonable(vars(obj))
    return str(obj)


def _instr_def_hit(rec: Any) -> dict[str, Any] | None:
    """Single-contract row from ``InstrumentDefMsg`` (common on ``schema=definition`` + ``raw_symbol``)."""
    if type(rec).__name__ != "InstrumentDefMsg":
        return None
    iid = getattr(rec, "instrument_id", None)
    raw = getattr(rec, "raw_symbol", None)
    if iid is None or raw is None:
        return None
    raw_s = _coerce_str(raw)
    if not raw_s:
        return None
    return {"instrument_id": int(iid), "raw_symbol": raw_s}


def _sym_mapping_dict(rec: Any) -> dict[str, Any] | None:
    if type(rec).__name__ != "SymbolMappingMsg":
        return None
    iid = getattr(rec, "instrument_id", None)
    if iid is None:
        return None
    out: dict[str, Any] = {
        "instrument_id": int(iid),
        "stype_in_symbol": _coerce_str(getattr(rec, "stype_in_symbol", "")),
        "stype_out_symbol": _coerce_str(getattr(rec, "stype_out_symbol", "")),
    }
    for name in ("stype_in", "stype_out", "start_ts", "end_ts"):
        v = getattr(rec, name, None)
        if v is not None:
            out[name] = int(v) if name.endswith("_ts") or name.startswith("stype_") else v
    return out


def _live_symbol_mappings(
    key: str,
    symbols: list[str],
    *,
    seconds: float,
    live_start: int,
    schema: str,
    st_in: str,
    max_maps: int,
) -> tuple[list[dict[str, Any]], bool]:
    """One ``db.Live`` subscription; collect ``SymbolMappingMsg`` rows (non-OCC / parent mode)."""
    maps: list[dict[str, Any]] = []
    n_map = 0
    client_holder: list[Any] = []

    def on_record(rec: Any) -> None:
        nonlocal n_map
        row = _sym_mapping_dict(rec)
        if row is None:
            return
        if n_map >= max_maps:
            return
        n_map += 1
        maps.append(row)

    client = db.Live(key=key)
    client_holder.append(client)
    client.subscribe(
        dataset=DATASET,
        schema=schema.strip(),
        symbols=symbols,
        stype_in=st_in,
        start=live_start,
    )
    client.add_callback(on_record)

    def _stop() -> None:
        try:
            client.stop()
        except Exception:
            pass

    t = threading.Timer(seconds, _stop)
    t.daemon = True
    live_ok = True
    t.start()
    try:
        try:
            client.start()
            client.block_for_close(timeout=max(60.0, seconds + 45.0))
        except BentoError:
            live_ok = False
    finally:
        t.cancel()
        try:
            client.terminate()
        except Exception:
            pass
    return maps, live_ok


def main() -> int:
    p = argparse.ArgumentParser(
        description=f"{DATASET} Live SymbolMappingMsg (default: underlyings.csv → ROOT.OPT, stype_in=parent; or one OCC positional).",
    )
    p.add_argument(
        "raw",
        nargs="?",
        default=None,
        help='single OPRA OCC, e.g. "AAPL  260501P00265000" (spaces collapsed; default stype_in=parent)',
    )
    p.add_argument(
        "--symbols-file",
        default=str(DEFAULT_UNDERLYINGS_CSV),
        help="underlyings list (one ticker per line); bare roots get .OPT (default: underlyings.csv next to this script)",
    )
    p.add_argument("--seconds", type=float, default=25.0, help="Live timeout (default: 25)")
    p.add_argument("--live-start", type=int, default=0, help="subscribe start= (default: 0)")
    p.add_argument(
        "--schema",
        default="definition",
        help="Live schema (default: definition)",
    )
    p.add_argument(
        "--stype-in",
        default="parent",
        help="Live subscribe stype_in (default: parent)",
    )
    p.add_argument("--max-maps", type=int, default=100_000)
    p.add_argument("--output", default="", help="write full JSON here instead of stdout")
    p.add_argument(
        "--csv",
        dest="csv_out",
        default=str(_default_csv_out("opra_pillar_unstripped.csv")),
        metavar="PATH",
        help="append symbol_mappings (default: YYYYMMDD/opra_pillar_unstripped.csv; empty to skip)",
    )
    p.add_argument(
        "--historical-fallback",
        action="store_true",
        help="if Live rejects the symbol, try Historical.symbology.resolve (same stype_in/out)",
    )
    args = p.parse_args()

    one_occ = args.raw is not None and str(args.raw).strip() != ""
    if one_occ:
        raw_in = str(args.raw)
        occ = _norm_sym(raw_in)
        compact = "".join(raw_in.split()).upper()
        root = compact[:-15].strip() if len(compact) >= 15 and compact[-9] in "CP" else _norm_sym(raw_in)
        symbols = [_parent(root)]
        sym_path: Path | None = None
    else:
        sym_arg = (args.symbols_file or "").strip()
        sym_path = Path(sym_arg) if sym_arg else DEFAULT_UNDERLYINGS_CSV
        if not sym_path.is_absolute():
            sym_path = sym_path.resolve()
        if not sym_path.is_file():
            print(f"error: not found: {sym_path}", file=sys.stderr)
            return 2
        seen: set[str] = set()
        symbols = []
        for line in _read_symbols(sym_path):
            q = _parent(line)
            if q and q not in seen:
                seen.add(q)
                symbols.append(q)
        occ = ""
        if not symbols:
            print(f"error: no symbols in {sym_path}", file=sys.stderr)
            return 2

    st_in = (args.stype_in or "").strip() or "parent"

    try:
        key = _api_key()
    except (RuntimeError, ValueError, FileNotFoundError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not key:
        print("error: empty API key", file=sys.stderr)
        return 1

    maps: list[dict[str, Any]] = []
    live_ok = True

    if one_occ:
        n_map = 0
        first_hit: list[dict[str, Any] | None] = [None]
        n_other_maps = 0
        client_holder: list[Any] = []

        def _occ_match(a: str, b_norm: str, b_key: str) -> bool:
            aa = _norm_sym(a)
            return aa == b_norm or _occ_key(aa) == b_key

        def on_record(rec: Any) -> None:
            nonlocal n_map, n_other_maps
            if one_occ and first_hit[0] is None:
                row = _sym_mapping_dict(rec)
                if row is not None:
                    if _occ_match(row["stype_in_symbol"], occ, _occ_key(occ)):
                        first_hit[0] = {**row, "via": "SymbolMappingMsg"}
                        try:
                            if client_holder:
                                client_holder[0].stop()
                        except Exception:
                            pass
                    else:
                        n_other_maps += 1
                    return
                dhit = _instr_def_hit(rec)
                if dhit is not None and _occ_match(dhit["raw_symbol"], occ, _occ_key(occ)):
                    first_hit[0] = {
                        "instrument_id": dhit["instrument_id"],
                        "stype_in_symbol": occ,
                        "stype_out_symbol": dhit["raw_symbol"],
                        "via": "InstrumentDefMsg",
                    }
                    try:
                        if client_holder:
                            client_holder[0].stop()
                    except Exception:
                        pass
                    return
                return

            row = _sym_mapping_dict(rec)
            if row is None:
                return
            if n_map >= args.max_maps:
                return
            n_map += 1
            maps.append(row)

        client = db.Live(key=key)
        client_holder.append(client)
        client.subscribe(
            dataset=DATASET,
            schema=args.schema.strip(),
            symbols=symbols,
            stype_in=st_in,
            start=args.live_start,
        )
        client.add_callback(on_record)

        def _stop() -> None:
            try:
                client.stop()
            except Exception:
                pass

        t = threading.Timer(args.seconds, _stop)
        t.daemon = True
        msg = (
            f"{DATASET} Live  schema={args.schema!r}  stype_in={st_in!r}  "
            f"OCC={symbols[0]!r}  ~{args.seconds:.0f}s  start={args.live_start}"
        )
        print(msg, file=sys.stderr, flush=True)
        print(f"  subscribe symbol (parent): {symbols[0]!r}", file=sys.stderr, flush=True)
        t.start()
        try:
            try:
                client.start()
                client.block_for_close(timeout=max(60.0, args.seconds + 45.0))
            except BentoError as exc:
                live_ok = False
                print(f"error: Live: {exc}", file=sys.stderr, flush=True)
        finally:
            t.cancel()
            try:
                client.terminate()
            except Exception:
                pass

    else:
        csv_fields = (
            "instrument_id",
            "stype_in_symbol",
            "stype_out_symbol",
            "stype_in",
            "stype_out",
            "start_ts",
            "end_ts",
        )
        csv_path_str = (args.csv_out or "").strip()
        csv_p: Path | None = None
        if csv_path_str:
            csv_p = Path(csv_path_str)
            if not csv_p.is_absolute():
                csv_p = (_repo_root() / csv_p).resolve()
            csv_p.parent.mkdir(parents=True, exist_ok=True)

        for idx, parent in enumerate(symbols):
            print(
                f"[{idx + 1}/{len(symbols)}] {DATASET} Live  parent={parent!r}  "
                f"schema={args.schema!r}  stype_in={st_in!r}  ~{args.seconds:.0f}s  start={args.live_start}",
                file=sys.stderr,
                flush=True,
            )
            maps_part, parent_live_ok = _live_symbol_mappings(
                key,
                [parent],
                seconds=args.seconds,
                live_start=args.live_start,
                schema=args.schema,
                st_in=st_in,
                max_maps=args.max_maps,
            )
            if not parent_live_ok:
                live_ok = False
                print(f"error: Live: failed for {parent!r}", file=sys.stderr, flush=True)
            maps.extend(maps_part)
            if csv_p is not None:
                _append_symbol_mapping_csv(csv_p, maps_part, csv_fields)
                print(f"  csv +{len(maps_part)} rows -> {csv_p}", file=sys.stderr, flush=True)

        if csv_p is not None:
            print(f"wrote/appended {csv_p} (total {len(maps)} rows)", file=sys.stderr, flush=True)

    if one_occ:
        hit = first_hit[0]
        if hit is None and args.historical_fallback:
            from datetime import date as _date, timedelta as _td

            h = db.Historical(key)
            start_s = (_date.today() - _td(days=1)).isoformat()
            for sym_try in (symbols[0], occ, _norm_sym(str(args.raw))):
                try:
                    r = h.symbology.resolve(
                        dataset=DATASET,
                        symbols=[sym_try],
                        stype_in=st_in,
                        stype_out="instrument_id",
                        start_date=start_s,
                        end_date=None,
                    )
                except BentoError:
                    continue
                inner = r.get("result") if isinstance(r, dict) else None
                if not isinstance(inner, dict) or not inner:
                    continue
                for _k, v in inner.items():
                    entries = v if isinstance(v, list) else [v]
                    for ent in entries:
                        if not isinstance(ent, dict):
                            continue
                        iid = ent.get("instrument_id") or ent.get("i")
                        if iid is not None:
                            hit = {
                                "instrument_id": int(iid),
                                "stype_in_symbol": sym_try,
                                "stype_out_symbol": str(ent.get("s") or ent.get("raw_symbol") or ""),
                                "via": "Historical.symbology.resolve",
                            }
                            break
                    if hit is not None:
                        break
                if hit is not None:
                    break
            else:
                hit = None
        if hit is None:
            hint = (
                "OPRA raw OCC must be OSI-shaped, e.g. AAPL + two spaces + 260501P00265000 "
                f"(subscribe tried {symbols[0]!r}). Or use --historical-fallback."
            )
            if live_ok:
                print(
                    f"error: no mapping for {occ!r} in {args.seconds:.0f}s "
                    f"({hint}; saw {n_other_maps} non-matching SymbolMappingMsg)",
                    file=sys.stderr,
                )
            else:
                print(f"error: {hint}", file=sys.stderr, flush=True)
            return 3
        print(hit["instrument_id"], file=sys.stdout)
        print(json.dumps(_to_jsonable(hit), indent=2), file=sys.stdout)
        return 0

    out = {
        "dataset": DATASET,
        "api": "live",
        "schema": args.schema.strip(),
        "seconds": args.seconds,
        "live_start": args.live_start,
        "stype_in": st_in,
        "symbols_file": str(sym_path) if sym_path else "",
        "symbols": symbols,
        "n_symbol_mappings": len(maps),
        "symbol_mappings": maps,
    }

    text = json.dumps(_to_jsonable(out), indent=2) + "\n"
    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        print(f"wrote {outp}", file=sys.stderr, flush=True)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
