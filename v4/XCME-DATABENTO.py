#!/usr/bin/env python3
"""CME Globex MDP 3.0 (dataset id ``GLBX.MDP3``) ``db.Live()``: ``SymbolMappingMsg`` (``stype_in_symbol`` → ``stype_out_symbol``, ``instrument_id``). API key: ``conf/config.ini`` ``api_key_es`` (or ``DATABENTO_API_KEY_ES``).

Docs: https://databento.com/docs/venues-and-datasets/glbx-mdp3 — Live/Historical ``dataset`` must be ``GLBX.MDP3`` (not ``glbx.mdp3``).

One symbol (positional): subscribe with ``stype_in=raw_symbol`` by default, stop on the first mapping.

  python databento_symbology_resolve_test_emini.py "ESH6"

No positional and no ``--symbols-file``: built-in ES weekly roots (see ``_ES_PARENT_ROOTS_CSV``): bare tokens become ``ROOT.OPT``; tokens that already contain ``.`` (e.g. ``ES.FUT``, ``ES.V.0``) are used as-is. ``stype_in=parent``, one Live subscribe per symbol, rows appended to ``YYYYMMDD/raw/XCME-DATABENTO.csv`` after each.

``--all-symbols``: single subscription to ``ALL_SYMBOLS`` with ``stype_in=raw_symbol`` (Historical-style definitions universe). Or pass ``--symbols-file`` for an explicit list (one batch, default ``stype_in=parent`` unless ``--stype-in`` is set).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

_HELPERS_DIR = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(_HELPERS_DIR))

from live_shutdown import register_live_client, unregister_live_client

import databento as db
from databento.common.constants import ALL_SYMBOLS
from databento.common.error import BentoError

DATASET = "GLBX.MDP3"
DEFAULT_LIVE_RETRIES = 3
DEFAULT_LIVE_RETRY_DELAY = 2.0

# Default no-arg mode: comma-separated. Bare roots → ``{root}.OPT``; any token with ``.`` → used as-is (futures / continuous).
_ES_PARENT_ROOTS_CSV = (
    "E1A,E1B,E1C,E1D,E2A,E2B,E2C,E2D,E3A,E3B,E3C,E3D,E4A,E4B,E4C,E4D,"
    "EW1,EW2,EW3,EW4,EW,E5A,E5B,E5C,E5D,ES,ES.FUT,ES.v.0,ES.v.1"
)

def _default_parent_opt_symbols() -> list[str]:
    parts = _ES_PARENT_ROOTS_CSV.replace("\n", "").split(",")
    out: list[str] = []
    for p in parts:
        s = p.strip().upper()
        if not s:
            continue
        out.append(s if "." in s else f"{s}.OPT")
    return out


def _append_symbol_mapping_csv(
    csv_p: Path,
    rows: list[dict[str, Any]],
    csv_fields: tuple[str, ...],
) -> None:
    """Append rows; write header only if the file is missing or empty."""
    has_body = csv_p.exists() and csv_p.stat().st_size > 0
    with csv_p.open("a", encoding="utf-8-sig", newline="") as cf:
        w = csv.DictWriter(cf, fieldnames=csv_fields, extrasaction="ignore")
        if not has_body:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in csv_fields})


def _repo_root() -> Path:
    from symbology_paths import repo_root

    return repo_root()


def _default_csv_out() -> Path:
    from symbology_paths import XCME_CSV, raw_csv

    return raw_csv(XCME_CSV)


def _read_symbols(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


def _norm_sym(s: str) -> str:
    return " ".join(s.split()).strip().upper()


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).split(b"\x00", 1)[0].decode("utf-8", errors="replace").rstrip()
    if isinstance(v, str):
        return v.rstrip()
    return str(v).rstrip()


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
    symbols: list[str] | str,
    *,
    seconds: float,
    live_start: int,
    schema: str,
    st_in: str,
    max_maps: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """One ``db.Live`` subscription; collect ``SymbolMappingMsg`` rows (``ALL_SYMBOLS`` or explicit list)."""
    maps: list[dict[str, Any]] = []
    n_map = 0

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
    register_live_client(client)
    live_ok = True
    err_msg: str | None = None
    t: threading.Timer | None = None
    try:
        try:
            client.subscribe(
                dataset=DATASET,
                schema=schema.strip(),
                symbols=symbols,
                stype_in=st_in,
                start=live_start,
            )
        except BentoError as exc:
            return [], False, str(exc)

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
            live_ok = False
            err_msg = str(exc)
    finally:
        if t is not None:
            t.cancel()
        unregister_live_client(client)
        try:
            client.terminate()
        except Exception:
            pass
    return maps, live_ok, err_msg


def _live_symbol_mappings_retry(
    key: str,
    symbols: list[str] | str,
    *,
    seconds: float,
    live_start: int,
    schema: str,
    st_in: str,
    max_maps: int,
    retries: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Retry ``_live_symbol_mappings`` on connect/session ``BentoError``."""
    if retries < 1:
        retries = 1
    last_maps: list[dict[str, Any]] = []
    for attempt in range(1, retries + 1):
        maps, ok, err = _live_symbol_mappings(
            key,
            symbols,
            seconds=seconds,
            live_start=live_start,
            schema=schema,
            st_in=st_in,
            max_maps=max_maps,
        )
        if ok:
            return maps, True
        last_maps = maps
        if attempt < retries:
            print(
                f"  Live attempt {attempt}/{retries} failed: {err}; "
                f"retry in {retry_delay:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(retry_delay)
        elif err:
            print(f"  Live failed after {retries} attempt(s): {err}", file=sys.stderr, flush=True)
    return last_maps, False


def main() -> int:
    p = argparse.ArgumentParser(
        description=f"{DATASET} Live SymbolMappingMsg (default: built-in parents one-by-one: bare roots as ROOT.OPT, dotted as-is e.g. ES.FUT; stype_in=parent; or --all-symbols; or --symbols-file; or one positional raw symbol).",
    )
    p.add_argument(
        "raw",
        nargs="?",
        default=None,
        help='single GLBX raw symbol, e.g. "ESH6" (default stype_in=raw_symbol)',
    )
    p.add_argument(
        "--symbols-file",
        default="",
        help="when no positional: load symbols from this file (one per line, as-is); if empty, use built-in parent list (ROOT.OPT + dotted parents) unless --all-symbols",
    )
    p.add_argument(
        "--all-symbols",
        action="store_true",
        help="with no positional and no --symbols-file: single Live subscribe to ALL_SYMBOLS, stype_in=raw_symbol",
    )
    p.add_argument("--seconds", type=float, default=25.0, help="Live timeout (default: 25)")
    p.add_argument(
        "--live-retries",
        type=int,
        default=DEFAULT_LIVE_RETRIES,
        help=f"Live connect/session retries per parent or batch (default: {DEFAULT_LIVE_RETRIES})",
    )
    p.add_argument(
        "--live-retry-delay",
        type=float,
        default=DEFAULT_LIVE_RETRY_DELAY,
        help=f"Seconds between Live retries (default: {DEFAULT_LIVE_RETRY_DELAY})",
    )
    p.add_argument("--live-start", type=int, default=0, help="subscribe start= (default: 0)")
    p.add_argument(
        "--schema",
        default="definition",
        help="Live schema (default: definition)",
    )
    p.add_argument(
        "--stype-in",
        default="",
        help="empty: one-symbol → raw_symbol; --all-symbols → raw_symbol; built-in / --symbols-file → parent",
    )
    p.add_argument("--max-maps", type=int, default=100_000)
    p.add_argument("--output", default="", help="write full JSON here instead of stdout")
    p.add_argument(
        "--csv",
        dest="csv_out",
        default=str(_default_csv_out()),
        metavar="PATH",
        help="write symbol_mappings to this CSV (default: YYYYMMDD/raw/XCME-DATABENTO.csv; empty to skip)",
    )
    p.add_argument(
        "--historical-fallback",
        action="store_true",
        help="if Live rejects the symbol, try Historical.symbology.resolve (same stype_in/out)",
    )
    args = p.parse_args()

    one_sym = args.raw is not None and str(args.raw).strip() != ""
    sym_path: Path | None = None
    target = ""
    builtin_parents_one_by_one = False
    if one_sym:
        raw_in = str(args.raw)
        target = _norm_sym(raw_in)
        symbols: list[str] | str = [target]
    else:
        sym_arg = (args.symbols_file or "").strip()
        if sym_arg:
            sym_path = Path(sym_arg)
            if not sym_path.is_absolute():
                sym_path = (_repo_root() / sym_path).resolve()
            if not sym_path.is_file():
                print(f"error: not found: {sym_path}", file=sys.stderr)
                return 2
            seen: set[str] = set()
            slist: list[str] = []
            for line in _read_symbols(sym_path):
                if line and line not in seen:
                    seen.add(line)
                    slist.append(line)
            if not slist:
                print("error: no symbols in file", file=sys.stderr)
                return 2
            symbols = slist
        elif args.all_symbols:
            symbols = ALL_SYMBOLS
        else:
            symbols = _default_parent_opt_symbols()
            builtin_parents_one_by_one = True

    st_in_explicit = (args.stype_in or "").strip()
    if one_sym:
        st_in = st_in_explicit or "raw_symbol"
    else:
        st_in = st_in_explicit or ("raw_symbol" if symbols == ALL_SYMBOLS else "parent")

    try:
        from config import get_api_key_es

        key = get_api_key_es()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    maps: list[dict[str, Any]] = []
    live_ok = True

    if one_sym:
        n_map = 0
        first_hit: list[dict[str, Any] | None] = [None]
        n_other_maps = 0
        client_holder: list[Any] = []

        def _sym_eq(a: str, b: str) -> bool:
            return _norm_sym(a) == _norm_sym(b)

        def on_record(rec: Any) -> None:
            nonlocal n_map, n_other_maps
            if one_sym and first_hit[0] is None:
                row = _sym_mapping_dict(rec)
                if row is not None:
                    if _sym_eq(row["stype_in_symbol"], target):
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
                if dhit is not None and _sym_eq(dhit["raw_symbol"], target):
                    first_hit[0] = {
                        "instrument_id": dhit["instrument_id"],
                        "stype_in_symbol": target,
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
        register_live_client(client)
        client_holder.append(client)
        t: threading.Timer | None = None
        try:
            try:
                client.subscribe(
                    dataset=DATASET,
                    schema=args.schema.strip(),
                    symbols=symbols,
                    stype_in=st_in,
                    start=args.live_start,
                )
            except BentoError as exc:
                live_ok = False
                print(f"error: Live subscribe: {exc}", file=sys.stderr, flush=True)
            else:
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
                    f"symbol={symbols[0]!r}  ~{args.seconds:.0f}s  start={args.live_start}"
                )
                print(msg, file=sys.stderr, flush=True)
                print(f"  subscribe raw_symbol: {symbols[0]!r}", file=sys.stderr, flush=True)
                t.start()
                try:
                    client.start()
                    client.block_for_close(timeout=max(60.0, args.seconds + 45.0))
                except BentoError as exc:
                    live_ok = False
                    print(f"error: Live: {exc}", file=sys.stderr, flush=True)
        finally:
            if t is not None:
                t.cancel()
            unregister_live_client(client)
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

        if builtin_parents_one_by_one:
            parents_list: list[str] = list(symbols)
            failed_parents: list[str] = []
            print(
                f"{DATASET} Live  built-in {len(parents_list)} parent(s)  "
                f"schema={args.schema!r}  stype_in={st_in!r}  one parent per session  "
                f"~{args.seconds:.0f}s  start={args.live_start}",
                file=sys.stderr,
                flush=True,
            )
            try:
                for i, parent in enumerate(parents_list, start=1):
                    print(
                        f"  parent {i}/{len(parents_list)}: {parent!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                    maps_part, parent_live_ok = _live_symbol_mappings_retry(
                        key,
                        [parent],
                        seconds=args.seconds,
                        live_start=args.live_start,
                        schema=args.schema,
                        st_in=st_in,
                        max_maps=args.max_maps,
                        retries=args.live_retries,
                        retry_delay=args.live_retry_delay,
                    )
                    if not parent_live_ok:
                        live_ok = False
                        failed_parents.append(parent)
                        print(
                            f"error: Live: skipping {parent!r} after {args.live_retries} attempt(s)",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    maps.extend(maps_part)
                    if csv_p is not None:
                        _append_symbol_mapping_csv(csv_p, maps_part, csv_fields)
                        print(
                            f"  csv +{len(maps_part)} rows -> {csv_p}",
                            file=sys.stderr,
                            flush=True,
                        )
            except KeyboardInterrupt:
                print("\ninterrupted: stopping parent loop", file=sys.stderr, flush=True)
                return 130
            if failed_parents:
                print(
                    f"warning: {len(failed_parents)}/{len(parents_list)} parent(s) skipped: "
                    f"{', '.join(failed_parents)}",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            sym_desc = ALL_SYMBOLS if symbols == ALL_SYMBOLS else f"{len(symbols)} symbols"
            print(
                f"{DATASET} Live  symbols={sym_desc!r}  "
                f"schema={args.schema!r}  stype_in={st_in!r}  ~{args.seconds:.0f}s  start={args.live_start}",
                file=sys.stderr,
                flush=True,
            )
            maps_part, parent_live_ok = _live_symbol_mappings_retry(
                key,
                symbols,
                seconds=args.seconds,
                live_start=args.live_start,
                schema=args.schema,
                st_in=st_in,
                max_maps=args.max_maps,
                retries=args.live_retries,
                retry_delay=args.live_retry_delay,
            )
            if not parent_live_ok:
                live_ok = False
                print(
                    f"error: Live: failed for {sym_desc!r} after {args.live_retries} attempt(s)",
                    file=sys.stderr,
                    flush=True,
                )
            maps.extend(maps_part)
            if csv_p is not None:
                _append_symbol_mapping_csv(csv_p, maps_part, csv_fields)
                print(
                    f"  csv +{len(maps_part)} rows -> {csv_p}",
                    file=sys.stderr,
                    flush=True,
                )

        if csv_p is not None:
            print(f"wrote/appended {csv_p} (total {len(maps)} rows)", file=sys.stderr, flush=True)

    if one_sym:
        hit = first_hit[0]
        if hit is None and args.historical_fallback:
            from datetime import date as _date, timedelta as _td

            h = db.Historical(key)
            start_s = (_date.today() - _td(days=1)).isoformat()
            for sym_try in (symbols[0], target, _norm_sym(str(args.raw))):
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
                f"GLBX.MDP3 raw_symbol must match the feed (subscribe tried {symbols[0]!r}). "
                "Or use --historical-fallback."
            )
            if live_ok:
                print(
                    f"error: no mapping for {target!r} in {args.seconds:.0f}s "
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
    elif not (args.csv_out or "").strip():
        sys.stdout.write(text)

    if not maps:
        return 1
    if not live_ok:
        print(
            f"warning: partial Live run ({len(maps)} symbol_mappings); see errors above)",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr, flush=True)
        raise SystemExit(130)
