"""Databento market data integration (wraps official databento SDK).

Matches v4-golang's internal/databento exactly:
  - dataset names: GLBX.MDP3 (XCME), OPRA.PILLAR (XCBO), EQUS.MINI (XNAS)
  - stype_in defaults: XCME=parent (raw_symbol if --all-symbols), XCBO=parent, XNAS=raw_symbol
  - --all-symbols in hist mode only works on EQUS.MINI (XNAS); GLBX.MDP3 and
    OPRA.PILLAR reject ALL_SYMBOLS at symbology.resolve
  - stype_out sent to API is always instrument_id
  - date range computed from metadata.get_dataset_range() minus a lookback window
  - output: YYYYMMDD/raw/{VENUE}-DATABENTO.csv with columns matching
    internal/databento/mapping.go's MappingColumns
"""
import datetime as dt
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import databento as db
import pandas as pd

from .. import config, paths, runner

ALL_SYMBOLS_SENTINEL = "ALL_SYMBOLS"

# symbology.resolve rejects ALL_SYMBOLS on most datasets with
# 422 symbology_all_symbols_with_incompatible_dataset -- the response is a single
# JSON blob and GLBX/OPRA carry ~1-2M instruments a day, so only EQUS.MINI (~13k)
# is accepted. Datasets listed here take the cheap resolve path for --all-symbols;
# everything else falls back to the definition schema (see _fetch_hist_definitions).
ALL_SYMBOLS_HIST_DATASETS = {"EQUS.MINI"}

MAPPING_COLUMNS = [
    "instrument_id",
    "stype_in_symbol",
    "stype_out_symbol",
    "stype_in",
    "stype_out",
    "start_ts",
    "end_ts",
]


@dataclass
class VenueConfig:
    """Databento venue configuration."""
    venue_name: str
    dataset: str


VENUE_CONFIGS = {
    "xcme": VenueConfig(venue_name="XCME", dataset="GLBX.MDP3"),
    "xcbo": VenueConfig(venue_name="XCBO", dataset="OPRA.PILLAR"),
    "xnas": VenueConfig(venue_name="XNAS", dataset="EQUS.MINI"),
}


def default_stype_in(venue: str, all_symbols: bool = False) -> str:
    """Per-venue default stype_in, matching Venue.DefaultStypeIn in v4-golang."""
    if venue == "xcme":
        return "raw_symbol" if all_symbols else "parent"
    elif venue == "xcbo":
        return "parent"
    elif venue == "xnas":
        return "raw_symbol"
    return "raw_symbol"


def resolve_symbols(
    venue: str,
    all_symbols: bool = False,
    symbols_file: Optional[str] = None,
) -> list[str]:
    """Resolve symbol list for a venue. No hardcoded defaults - basket CSV required."""
    if all_symbols:
        return [ALL_SYMBOLS_SENTINEL]

    # Use explicit symbols file if provided
    if symbols_file:
        path = Path(symbols_file)
        if not path.exists():
            raise FileNotFoundError(f"Symbols file not found: {path}")
        with open(path) as f:
            symbols = [line.strip() for line in f if line.strip()]
    else:
        # Require basket CSV file
        venue_upper = venue.upper()
        basket_csv = paths.baskets_dir() / f"{venue_upper}.csv"
        if not basket_csv.exists():
            raise FileNotFoundError(f"Symbol basket CSV not found: {basket_csv}")
        with open(basket_csv) as f:
            symbols = [line.strip() for line in f if line.strip()]

    # XCME/XCBO use parent symbol format: append .OPT to bare roots
    # (index parents like .SPX and symbols already suffixed with .OPT/.FUT/.SPOT stay as-is)
    if venue in ("xcme", "xcbo"):
        symbols = [
            s if s.startswith(".") or s.endswith((".OPT", ".FUT", ".SPOT"))
            else f"{s}.OPT"
            for s in symbols
        ]

    return symbols


def resolve_hist_range(
    client: db.Historical,
    dataset: str,
    as_of: str,
    lookback_days: int,
    explicit_range: Optional[str] = None,
) -> tuple[str, str]:
    """
    Compute (start_date, end_date) for the hist resolve request.

    If explicit_range is given (16-digit YYYYMMDDYYYYMMDD, from --range), use it
    directly: from=start (inclusive), to=end+1day (exclusive UTC midnight),
    still clamped to the dataset's actual available window.

    Otherwise matches ResolveHistRange in v4-golang: end = asOf+1day (exclusive
    UTC midnight, clamped to dataset's actual available end), start = end -
    lookback_days (clamped to dataset's actual available start).
    """
    dataset_range = client.metadata.get_dataset_range(dataset=dataset)
    first = dt.datetime.strptime(dataset_range["start"][:10], "%Y-%m-%d").date()
    last = dt.datetime.strptime(dataset_range["end"][:10], "%Y-%m-%d").date()

    if explicit_range:
        from_str, to_str = runner.parse_hist_range(explicit_range)
        start = dt.datetime.strptime(from_str, "%Y%m%d").date()
        end_day = min(dt.datetime.strptime(to_str, "%Y%m%d").date(), last)
        end = end_day + dt.timedelta(days=1)  # exclusive UTC midnight
        if start < first:
            start = first
        return start.isoformat(), end.isoformat()

    as_of_date = dt.datetime.strptime(as_of, "%Y%m%d").date()
    end_day = min(as_of_date, last)
    end = end_day + dt.timedelta(days=1)  # exclusive UTC midnight
    start = end - dt.timedelta(days=lookback_days)
    if start < first:
        start = first

    return start.isoformat(), end.isoformat()


def download(opts: runner.Opts, venue: str, mode: str) -> None:
    """
    Download Databento data (hist or live).
    venue: 'xcme', 'xcbo', 'xnas'
    mode: 'hist' or 'live'
    """
    if venue not in VENUE_CONFIGS:
        raise ValueError(f"Unknown venue: {venue}")

    venue_cfg = VENUE_CONFIGS[venue]
    cfg = config.load_databento()

    # Select API key based on venue
    api_key = cfg.keys.get(venue_cfg.venue_name, "")
    if not api_key:
        raise ValueError(
            f"No Databento API key configured for {venue} ({venue_cfg.venue_name}); "
            f"set key_{venue_cfg.venue_name} in conf/keys.ini"
        )

    # Resolve symbols
    symbols = resolve_symbols(
        venue,
        all_symbols=opts.all_symbols,
        symbols_file=opts.symbols_file,
    )
    stype_in = opts.stype_in or default_stype_in(venue, opts.all_symbols)

    # symbology.resolve only accepts ALL_SYMBOLS on small datasets; elsewhere the
    # definition schema is the supported route to the full instrument universe.
    use_definitions = (
        opts.all_symbols
        and mode == "hist"
        and venue_cfg.dataset not in ALL_SYMBOLS_HIST_DATASETS
    )
    if use_definitions:
        # The definition path writes record.raw_symbol into stype_in_symbol, so the
        # stype_in column has to say raw_symbol or the CSV mislabels its own contents
        # (xcbo would otherwise carry the "parent" default). ALL_SYMBOLS bypasses
        # symbol resolution anyway -- raw_symbol and parent return identical records.
        stype_in = "raw_symbol"

    if opts.dry_run:
        route = "definition schema" if use_definitions else "symbology.resolve"
        print(f"DRY RUN: Would download {venue} {mode} via {route} "
              f"stype_in={stype_in} for symbols: {symbols}")
        return

    raw_dir = paths.raw_dir(opts.date_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    output_csv = paths.databento_raw_csv(opts.date_dir, venue)
    temp_csv = output_csv.with_suffix(".tmp.csv")

    try:
        if mode == "hist":
            client = db.Historical(key=api_key)
            if use_definitions:
                rows = _fetch_definitions(
                    client, venue_cfg, stype_in, opts.date_dir,
                    cfg.hist_lookback_days, opts.hist_range,
                )
            else:
                batch_size = cfg.hist_batch_size if opts.batch_size is None else opts.batch_size
                rows = _fetch_hist(
                    client, venue_cfg, symbols, stype_in, opts.date_dir,
                    cfg.hist_lookback_days, opts.hist_range, batch_size,
                    cfg.hist_retries, cfg.hist_retry_delay_sec,
                )
        elif mode == "live":
            rows = _fetch_live(
                api_key, venue_cfg, symbols, stype_in, opts.live_start,
                cfg.live_seconds, cfg.max_maps, cfg.live_retries, cfg.live_retry_delay_sec,
            )
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if rows:
            df = pd.DataFrame(rows, columns=MAPPING_COLUMNS)
            df.to_csv(temp_csv, index=False, encoding="utf-8-sig")
            temp_csv.replace(output_csv)  # replace, not rename: rename fails on Windows if target exists
            print(f"Wrote {len(df)} rows to {output_csv}")
        else:
            print(f"No data retrieved for {venue} {mode}")

    except Exception:
        if temp_csv.exists():
            temp_csv.unlink()
        raise


def _fetch_hist(
    client: db.Historical,
    venue_cfg: VenueConfig,
    symbols: list[str],
    stype_in: str,
    as_of: str,
    lookback_days: int,
    explicit_range: Optional[str] = None,
    batch_size: int = 5,
    retries: int = 3,
    retry_delay_sec: float = 5.0,
) -> list[dict]:
    """
    Chunked symbology.resolve() calls, batch_size symbols per request.

    One request for the whole basket stalls on parent symbology: a single OPRA/GLBX
    parent expands to thousands of contracts, so a whole basket in one call is a
    multi-hundred-thousand-row response the gateway will 504 on. Chunking keeps each
    request bounded; batch_size <= 0 sends everything in one request (the old
    behaviour).

    Each batch also retries independently: the 504s are load-dependent, not purely
    size-dependent (a 5-parent batch has been seen to succeed while a 3-parent one
    timed out), so a smaller batch alone is not enough to make this reliable.
    """
    start_date, end_date = resolve_hist_range(client, venue_cfg.dataset, as_of, lookback_days, explicit_range)

    if batch_size > 0:
        chunks = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    else:
        chunks = [symbols]

    print(f"  Resolving {venue_cfg.venue_name} hist: {len(symbols)} symbol(s) in {len(chunks)} batch(es) "
          f"of up to {batch_size if batch_size > 0 else len(symbols)}, "
          f"stype_in={stype_in}, start={start_date} (no end_date, defaults to latest available)")

    rows: list[dict] = []
    not_found: list[str] = []
    failed: list[tuple[list[str], Exception]] = []

    attempts = max(retries, 1)
    for i, chunk in enumerate(chunks, 1):
        result = None
        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                result = client.symbology.resolve(
                    dataset=venue_cfg.dataset,
                    symbols=chunk,
                    stype_in=stype_in,
                    stype_out="instrument_id",
                    start_date=start_date,
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < attempts:
                    delay = retry_delay_sec * attempt  # linear backoff
                    print(f"    batch {i}/{len(chunks)} attempt {attempt}/{attempts} failed: {e}; "
                          f"retry in {delay:.0f}s")
                    time.sleep(delay)

        if last_err is not None or result is None:
            print(f"    batch {i}/{len(chunks)} {chunk}: FAILED after {attempts} attempt(s): {last_err}")
            failed.append((chunk, last_err))
            continue

        chunk_rows = []
        mappings = result.get("result", {})
        for stype_in_symbol, entries in mappings.items():
            for entry in entries:
                chunk_rows.append({
                    "instrument_id": entry.get("s", ""),
                    "stype_in_symbol": stype_in_symbol,
                    "stype_out_symbol": entry.get("s", ""),
                    "stype_in": stype_in,
                    "stype_out": "instrument_id",
                    "start_ts": entry.get("d0", ""),
                    "end_ts": entry.get("d1", ""),
                })

        rows.extend(chunk_rows)
        not_found.extend(result.get("not_found", []))
        print(f"    batch {i}/{len(chunks)} {chunk}: {len(chunk_rows)} row(s) "
              f"(running total {len(rows)})")

    if not_found:
        print(f"    Warning: not found: {not_found}")

    # Every batch failing is a hard error: writing an empty CSV over a good one
    # would look like a clean run that legitimately found nothing.
    if failed and not rows:
        raise RuntimeError(
            f"all {len(chunks)} symbology.resolve batch(es) failed; first error: {failed[0][1]}"
        )
    if failed:
        print(f"    Warning: {len(failed)}/{len(chunks)} batch(es) failed, output is partial: "
              f"{[c for c, _ in failed]}")

    return rows


def _fetch_definitions(
    client: db.Historical,
    venue_cfg: VenueConfig,
    stype_in: str,
    as_of: str,
    lookback_days: int,
    explicit_range: Optional[str] = None,
) -> list[dict]:
    """
    ALL_SYMBOLS via the `definition` schema, for datasets symbology.resolve refuses.

    Emits the same MAPPING_COLUMNS shape resolve() produces, so normalize-databento
    consumes it unchanged: stype_in_symbol carries the ticker text and
    stype_out_symbol the numeric id, which is what _resolve_symbol_id_fallback
    expects for a stype_out="instrument_id" row.

    Only the single most recent session is requested, not the full lookback window:
    definitions are a daily snapshot of the instrument universe, so N days would be
    N copies of the same instruments (GLBX alone is ~1.1M records / ~577 MB a day).
    Records are deduped on instrument_id since definitions restate intraday.
    """
    # No `end` on purpose: the API forward-fills it from `start` at the request's
    # resolution, so a day-resolution start with no end is exactly one session.
    # The forward-filled end is still bounds-checked though, so `start` has to be the
    # last *complete* session or the implied end lands past what's published and the
    # request 422s with data_end_after_available_end. The dataset's available end is a
    # timestamp (e.g. 2026-08-12 05:20Z, mid-session), so its own date is incomplete --
    # step back one day, which also holds when the end is exactly midnight since the
    # forward-filled end is exclusive.
    available_end = pd.Timestamp(client.metadata.get_dataset_range(dataset=venue_cfg.dataset)["end"])
    start_s = str(available_end.date() - dt.timedelta(days=1))

    print(f"  Resolving {venue_cfg.venue_name} hist: ALL_SYMBOLS via definition schema, "
          f"stype_in={stype_in}, start={start_s} (no end, one session)")

    count = client.metadata.get_record_count(
        dataset=venue_cfg.dataset, symbols=ALL_SYMBOLS_SENTINEL, stype_in=stype_in,
        schema="definition", start=start_s,
    )
    size = client.metadata.get_billable_size(
        dataset=venue_cfg.dataset, symbols=ALL_SYMBOLS_SENTINEL, stype_in=stype_in,
        schema="definition", start=start_s,
    )
    print(f"    {count:,} record(s), {size / 1e6:.1f} MB to download...")

    store = client.timeseries.get_range(
        dataset=venue_cfg.dataset,
        symbols=ALL_SYMBOLS_SENTINEL,
        stype_in=stype_in,
        schema="definition",
        start=start_s,
    )

    by_id: dict[int, dict] = {}
    for record in store:
        if not isinstance(record, db.InstrumentDefMsg):
            continue
        by_id[record.instrument_id] = {
            "instrument_id": record.instrument_id,
            "stype_in_symbol": record.raw_symbol,
            "stype_out_symbol": record.instrument_id,
            "stype_in": stype_in,
            "stype_out": "instrument_id",
            "start_ts": record.pretty_activation,
            "end_ts": record.pretty_expiration,
        }

    print(f"    {len(by_id):,} unique instrument(s) after dedupe on instrument_id")
    return list(by_id.values())


def _fetch_live(
    api_key: str,
    venue_cfg: VenueConfig,
    symbols: list[str],
    stype_in: str,
    live_start: Optional[str],
    live_seconds: float,
    max_maps: int,
    retries: int,
    retry_delay_sec: float,
) -> list[dict]:
    """
    Subscribe one symbol at a time (separate db.Live session per symbol) so a single
    symbol that Databento can't resolve (e.g. no live contract right now) doesn't kill
    the whole batch. Each symbol gets its own retry budget; failures are logged and
    skipped rather than aborting the run.
    """
    retries = max(retries, 1)
    all_rows: list[dict] = []

    for symbol in symbols:
        sym_rows: list[dict] = []
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                sym_rows = _fetch_live_once(
                    api_key, venue_cfg, [symbol], stype_in, live_start, live_seconds, max_maps,
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < retries:
                    print(f"  Live attempt {attempt}/{retries} failed for {symbol}: {e}; retry in {retry_delay_sec:.0f}s")
                    time.sleep(retry_delay_sec)

        if last_err is not None:
            print(f"    Warning: {venue_cfg.venue_name} live failed for {symbol}: {last_err}")
            continue

        print(f"    {symbol}: {len(sym_rows)} mapping(s)")
        all_rows.extend(sym_rows)

    return all_rows


def _fetch_live_once(
    api_key: str,
    venue_cfg: VenueConfig,
    symbols: list[str],
    stype_in: str,
    live_start: Optional[str],
    live_seconds: float,
    max_maps: int,
) -> list[dict]:
    if not symbols:
        raise ValueError("no symbols to subscribe")

    client = db.Live(key=api_key)
    try:
        client.subscribe(
            dataset=venue_cfg.dataset,
            schema="definition",
            symbols=symbols,
            stype_in=stype_in,
            start=live_start or None,
        )

        timeout = live_seconds if live_seconds > 0 else 25.0
        stop_timer = threading.Timer(timeout, client.stop)
        stop_timer.start()

        rows: list[dict] = []
        try:
            for record in client:
                if not isinstance(record, db.SymbolMappingMsg):
                    continue
                rows.append({
                    "instrument_id": record.instrument_id,
                    "stype_in_symbol": record.stype_in_symbol,
                    "stype_out_symbol": record.stype_out_symbol,
                    "stype_in": stype_in,
                    "stype_out": "instrument_id",
                    "start_ts": record.pretty_start_ts,
                    "end_ts": record.pretty_end_ts,
                })
                if max_maps > 0 and len(rows) >= max_maps:
                    break
        finally:
            stop_timer.cancel()

        return rows
    finally:
        client.stop()
