"""Databento market data integration (wraps official databento SDK).

Matches v4-golang's internal/databento exactly:
  - dataset names: GLBX.MDP3 (XCME), OPRA.PILLAR (XCBO), EQUS.MINI (XNAS)
  - stype_in defaults: XCME=parent (raw_symbol if --all-symbols), XCBO=parent, XNAS=raw_symbol
  - stype_out sent to API is always instrument_id
  - date range computed from metadata.get_dataset_range() minus a lookback window
  - output: YYYYMMDD/raw/{VENUE}-DATABENTO.csv with columns matching
    internal/databento/mapping.go's MappingColumns
"""
import csv
import datetime as dt
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import databento as db

from .. import config, paths, runner

ALL_SYMBOLS_SENTINEL = "ALL_SYMBOLS"

# Symbols per symbology.resolve() call. The gateway answers per request, not per
# symbol, so a whole basket in one call times out once it expands far enough --
# 506 OPRA parents (~840 contracts each) returns 504 every time.
HIST_RESOLVE_BATCH = 5
HIST_RESOLVE_RETRIES = 3
HIST_RESOLVE_RETRY_DELAY_SEC = 4

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
    # OPRA reassigns instrument_id every trading day: resolving against any date
    # other than the latest complete session returns a token space that shares
    # almost nothing with the live feed. Measured on NVDA.OPT against live on
    # 2026-08-04 -- start=08-03 matched 3818/3818, start=07-31 matched 1/3758,
    # start=07-29 matched 0/3606. Not a decay curve, a cliff.
    #
    # Worse than a miss: ids that appear on both days mostly point at *different*
    # contracts (8542 of 8551 across the full 8-parent basket), so a token join
    # silently attributes ticks to the wrong strike rather than dropping them.
    #
    # GLBX/EQUS ids are stable across dates (XCME: 27596/27596 hist-vs-live), and
    # there the lookback window is load-bearing -- it picks up recently expired
    # contracts the live definition stream no longer announces (59505 vs 43109
    # symbols). So this is opt-in per venue, not a global policy change.
    hist_pin_latest_session: bool = False


VENUE_CONFIGS = {
    "xcme": VenueConfig(venue_name="XCME", dataset="GLBX.MDP3"),
    "xcbo": VenueConfig(venue_name="XCBO", dataset="OPRA.PILLAR", hist_pin_latest_session=True),
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
    pin_latest_session: bool = False,
) -> tuple[str, str]:
    """
    Compute (start_date, end_date) for the hist resolve request.

    If explicit_range is given (16-digit YYYYMMDDYYYYMMDD, from --range), use it
    directly: from=start (inclusive), to=end+1day (exclusive UTC midnight),
    still clamped to the dataset's actual available window. An explicit range is
    an operator override and wins over pin_latest_session.

    If pin_latest_session, ignore lookback_days and resolve against the latest
    complete session only -- required for OPRA, see VenueConfig.

    Otherwise matches ResolveHistRange in v4-golang: end = asOf+1day (exclusive
    UTC midnight, clamped to dataset's actual available end), start = end -
    lookback_days (clamped to dataset's actual available start).

    Note dataset_range["end"] is an EXCLUSIVE bound: while 2026-08-03 was the
    last session with data, the API reported end=2026-08-04T00:00:00Z. Treating
    it as inclusive is what makes a naive lookback_days=1 resolve to a start_date
    the API rejects with 422 data_start_date_after_available_end_date. Only the
    pinned branch below corrects for this -- the lookback branch keeps the old
    arithmetic verbatim so XCME/XNAS output is byte-identical to before.
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

    if pin_latest_session:
        # `last` is the exclusive end, so the latest session with data is the day
        # before it. Resolve start==that day, end==the exclusive bound itself.
        start = max(last - dt.timedelta(days=1), first)
        return start.isoformat(), last.isoformat()

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

    if opts.dry_run:
        print(f"DRY RUN: Would download {venue} {mode} stype_in={stype_in} for symbols: {symbols}")
        return

    raw_dir = paths.raw_dir(opts.date_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    output_csv = paths.databento_raw_csv(opts.date_dir, venue)
    # PID-scoped so two runs of the same venue cannot share a staging file. A
    # fixed ".tmp.csv" made concurrent runs fight over one path: whichever
    # finished first renamed it away, and the other died at its own rename with
    # "No such file or directory" after resolving the whole basket.
    temp_csv = output_csv.with_suffix(f".tmp.{os.getpid()}.csv")

    if mode == "hist":
        client = db.Historical(key=api_key)
        batches = _iter_hist_batches(
            client, venue_cfg, symbols, stype_in, opts.date_dir,
            cfg.hist_lookback_days, opts.hist_range,
        )
    elif mode == "live":
        batches = _iter_live_batches(
            api_key, venue_cfg, symbols, stype_in, opts.live_start,
            cfg.live_seconds, cfg.max_maps, cfg.live_retries, cfg.live_retry_delay_sec,
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    # Stage into .tmp.csv, appending each gateway response as it arrives, then
    # rename onto the real name once the whole basket is in. Nothing is held in
    # memory: a 506-parent OPRA basket resolves to ~500k rows, which used to be
    # kept as dicts and again as a DataFrame before a single write at the end.
    #
    # Staging keeps the output atomic -- readers only ever see a complete basket,
    # never a run in progress. The staging path is unique per process, so there
    # is nothing stale to clear here: deleting a fixed temp path at startup would
    # itself destroy a concurrent run's work in progress.
    total = 0
    try:
        with open(temp_csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=MAPPING_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            fh.flush()
            for batch_rows in batches:
                if not batch_rows:
                    continue
                writer.writerows(batch_rows)
                fh.flush()  # every response is durable before the next request
                total += len(batch_rows)
    except Exception:
        # Keep the partial .tmp.csv rather than deleting it: with one request per
        # symbol a late failure can be an hour of work, and the real output is
        # untouched anyway because the rename below never runs.
        if total:
            print(f"  Failed after {total} row(s); partial output kept at {temp_csv}")
        raise

    if total:
        temp_csv.replace(output_csv)
        print(f"Wrote {total} rows to {output_csv}")
    else:
        # A header-only file would look like a valid empty basket downstream.
        temp_csv.unlink(missing_ok=True)
        print(f"No data retrieved for {venue} {mode}")


def _iter_hist_batches(
    client: db.Historical,
    venue_cfg: VenueConfig,
    symbols: list[str],
    stype_in: str,
    as_of: str,
    lookback_days: int,
    explicit_range: Optional[str] = None,
):
    """Batched symbology.resolve(), yielding each response's rows as it arrives.

    Resolving a large basket in a single call times out: 506 OPRA parents
    (each expanding to ~840 contracts) reliably returns
    "504 The remote gateway timed out". Batching keeps each request small
    enough to answer; yielding rather than accumulating keeps the caller free
    to append them to the CSV without ever holding the full basket in memory.
    """
    pinned = venue_cfg.hist_pin_latest_session and not explicit_range
    start_date, end_date = resolve_hist_range(
        client, venue_cfg.dataset, as_of, lookback_days, explicit_range,
        pin_latest_session=venue_cfg.hist_pin_latest_session,
    )

    window = "pinned to latest session" if pinned else f"lookback {lookback_days}d"
    batches = [symbols[i:i + HIST_RESOLVE_BATCH] for i in range(0, len(symbols), HIST_RESOLVE_BATCH)]
    print(f"  Resolving {venue_cfg.venue_name} hist: {len(symbols)} symbol(s) in {len(batches)} batch(es) "
          f"of {HIST_RESOLVE_BATCH}, stype_in={stype_in}, start={start_date} "
          f"({window}; no end_date, defaults to latest available)")

    total = 0
    not_found: list[str] = []
    for i, batch in enumerate(batches, 1):
        batch_rows, batch_nf = _resolve_batch(
            client, venue_cfg, batch, stype_in, start_date,
        )
        total += len(batch_rows)
        not_found.extend(batch_nf)
        print(f"    batch {i}/{len(batches)}: {len(batch)} symbol(s) -> "
              f"{len(batch_rows)} contract(s), running total {total}", flush=True)
        yield batch_rows

    if not_found:
        print(f"    Warning: not found: {not_found}")


def _resolve_batch(
    client: db.Historical,
    venue_cfg: VenueConfig,
    batch: list[str],
    stype_in: str,
    start_date: str,
) -> tuple[list[dict], list[str]]:
    """Resolve one batch, retrying then halving on failure.

    A 504 is a function of how much the batch expands, not how many symbols it
    holds -- a handful of mega-cap option parents can time out where a hundred
    thin ones do not. Retrying the same batch often works; when it doesn't,
    halving isolates the heavy symbol instead of losing the whole batch. Only a
    single symbol that still fails is given up on, and it is reported.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, HIST_RESOLVE_RETRIES + 1):
        try:
            result = client.symbology.resolve(
                dataset=venue_cfg.dataset,
                symbols=batch,
                stype_in=stype_in,
                stype_out="instrument_id",
                start_date=start_date,
            )
            rows = []
            for stype_in_symbol, entries in result.get("result", {}).items():
                for entry in entries:
                    rows.append({
                        "instrument_id": entry.get("s", ""),
                        "stype_in_symbol": stype_in_symbol,
                        "stype_out_symbol": entry.get("s", ""),
                        "stype_in": stype_in,
                        "stype_out": "instrument_id",
                        "start_ts": entry.get("d0", ""),
                        "end_ts": entry.get("d1", ""),
                    })
            return rows, list(result.get("not_found", []))
        except Exception as e:
            last_err = e
            if attempt < HIST_RESOLVE_RETRIES:
                print(f"      attempt {attempt}/{HIST_RESOLVE_RETRIES} failed for "
                      f"{len(batch)} symbol(s): {str(e)[:80]}; retrying in "
                      f"{HIST_RESOLVE_RETRY_DELAY_SEC}s")
                time.sleep(HIST_RESOLVE_RETRY_DELAY_SEC)

    if len(batch) == 1:
        print(f"      Warning: giving up on {batch[0]}: {str(last_err)[:100]}")
        return [], list(batch)

    mid = len(batch) // 2
    print(f"      splitting {len(batch)} symbol(s) into {mid}/{len(batch) - mid} after "
          f"{HIST_RESOLVE_RETRIES} failed attempts")
    left_rows, left_nf = _resolve_batch(client, venue_cfg, batch[:mid], stype_in, start_date)
    right_rows, right_nf = _resolve_batch(client, venue_cfg, batch[mid:], stype_in, start_date)
    return left_rows + right_rows, left_nf + right_nf


def _iter_live_batches(
    api_key: str,
    venue_cfg: VenueConfig,
    symbols: list[str],
    stype_in: str,
    live_start: Optional[str],
    live_seconds: float,
    max_maps: int,
    retries: int,
    retry_delay_sec: float,
):
    """
    Subscribe one symbol at a time (separate db.Live session per symbol) so a single
    symbol that Databento can't resolve (e.g. no live contract right now) doesn't kill
    the whole batch. Each symbol gets its own retry budget; failures are logged and
    skipped rather than aborting the run.

    Yields each symbol's rows so the caller can append them to the CSV as they
    arrive instead of holding every symbol's mappings in memory.
    """
    retries = max(retries, 1)

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

        print(f"    {symbol}: {len(sym_rows)} mapping(s)", flush=True)
        yield sym_rows


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
