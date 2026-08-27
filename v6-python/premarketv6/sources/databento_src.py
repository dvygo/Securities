"""Databento market data integration (wraps official databento SDK).

Venue wiring:
  - dataset names: GLBX.MDP3 (XCME), OPRA.PILLAR (XCBO), EQUS.MINI (XNAS)
  - the venue table itself is config.ini's [EXCHANGE:<CODE>] sections, not code:
    dataset, stype_in, schema, --all-symbols default and the clamp/readiness
    knobs all live there (premarketv6.config.load_exchanges)
  - stype_in defaults: XCME=parent (raw_symbol if --all-symbols), XCBO=parent, XNAS=raw_symbol
  - --all-symbols is on by default for all three, and in hist mode it always
    means the definition schema fetched via a batch job
    (_download_definitions_via_batch), landing as .dbn.zst -- no venue takes
    symbology.resolve for ALL_SYMBOLS any more
  - symbology.resolve is still the route for basket downloads (--no-all-symbols
    or --symbols-file), which cannot carry instrument_class
  - stype_out sent to API is always instrument_id
  - date range computed from metadata.get_dataset_range() minus a lookback window
  - output:
      - definition schema (any venue, --all-symbols): YYYYMMDD/{VENUE}/*.dbn.zst,
        via paths.manual_venue_dir -- no CSV, read directly by normalize
      - basket downloads: YYYYMMDD/raw/{VENUE}-DATABENTO.csv, columns matching
        internal/databento/mapping.go's MappingColumns
"""
import csv
import datetime as dt
import os
import re
import threading
import time
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

# EQUS.MINI used to be carved out here: symbology.resolve rejects ALL_SYMBOLS on
# most datasets with 422 symbology_all_symbols_with_incompatible_dataset (the
# response is a single JSON blob and GLBX/OPRA carry ~1-2M instruments a day),
# but EQUS at ~13k was small enough to be accepted, so XNAS took the cheap
# resolve route. That carve-out is gone: --all-symbols now means the definition
# schema via a batch job for every venue, so all three land as .dbn.zst.
#
# The cost of the carve-out was instrument_class. symbology.resolve returns ids
# and dates and nothing else, so XNAS shipped 13,195 rows with the column blank
# and the normalizer fell back to parsing symbol strings, while XCBO and XCME
# read it straight off the InstrumentDefMsg records. EQUS definitions are ~13k
# records against OPRA's ~2M, so the job is quick.

MAPPING_COLUMNS = [
    "instrument_id",
    "stype_in_symbol",
    "stype_out_symbol",
    "stype_in",
    "stype_out",
    "start_ts",
    "end_ts",
    # Only the definition path can fill this -- symbology.resolve returns ids and
    # dates, never an instrument's class. Left empty on the resolve path, which is
    # what tells the normalizer to fall back to parsing the symbol string.
    "instrument_class",
]


def _def_value(record, name: str) -> str:
    """One InstrumentDefMsg field as CSV text.

    Everything is stringified, including the numerics: the CSV is untyped text and
    the whole pipeline reads it back with dtype=str, so converting here would only
    create a second opinion about what a blank cell is -- and pandas' inference is
    what used to render instrument ids as "637543226.0".

    Prices stay in Databento's 1e-9 fixed point rather than using the pretty_*
    accessors, matching the normalized `strike`/`multiplier` columns, which are
    fixed-point at the same scale. Timestamps stay as nanoseconds since the epoch;
    their human-readable forms are already carried in start_ts/end_ts.

    The char enums (security_update_action, match_algorithm, leg_side,
    leg_instrument_class, user_defined_instrument) stringify to their
    one-character code, not their repr -- str(SecurityUpdateAction.ADD) is "A",
    not "<SecurityUpdateAction.ADD: 'A'>". Verified against databento-dbn 0.63.
    """
    value = getattr(record, name, None)
    if value is None:
        return ""
    return str(value)


# Venue table, read from config.ini's [EXCHANGE:<CODE>] sections. There is no
# built-in fallback: the old hardcoded VenueConfig table and config.ini used to
# describe the same venues independently, and could disagree without anyone
# noticing. config.ini is now the only description.
#
# Only databento-fed exchanges land here -- [EXCHANGE:XNSE]/[EXCHANGE:XBOM] are
# feed=fyers and belong to sources/fyers_src.py.
#
# Notes that used to live on the dataclass, kept because they are the evidence
# behind two of the config values:
#
# hist_pin_latest_session (true for XCBO): OPRA reassigns instrument_id every
# trading day, so resolving against any date other than the latest complete
# session returns a token space sharing almost nothing with the live feed.
# Measured on NVDA.OPT against live on 2026-08-04 -- start=08-03 matched
# 3818/3818, start=07-31 matched 1/3758, start=07-29 matched 0/3606. Not a decay
# curve, a cliff. Worse than a miss: ids present on both days mostly point at
# *different* contracts (8542 of 8551 across the full 8-parent basket), so a
# token join silently attributes ticks to the wrong strike rather than dropping
# them. GLBX/EQUS ids are stable across dates (XCME: 27596/27596 hist-vs-live)
# and there the lookback window is load-bearing -- it picks up recently expired
# contracts the live definition stream no longer announces (59505 vs 43109
# symbols). Hence per-venue, not global.
#
# definition_ready_ratio: the upper clamp in _download_definitions_via_batch
# only stops the API rejecting the range -- it cannot tell a complete session
# from one whose definitions have not published yet, and both produce a file.
# The measured publish curves are in config.ini next to the values themselves.
VENUE_CONFIGS: dict[str, config.ExchangeCfg] = {
    venue: exchange_cfg
    for venue, exchange_cfg in config.load_exchanges().items()
    if exchange_cfg.feed == "databento"
}


def default_stype_in(venue: str, all_symbols: bool = False) -> str:
    """Per-venue default stype_in, from [EXCHANGE:<CODE>].

    XCME is the one venue where the two differ: `parent` for a basket download,
    `raw_symbol` once ALL_SYMBOLS is in play.
    """
    exchange_cfg = VENUE_CONFIGS.get(venue)
    if exchange_cfg is None:
        return "raw_symbol"
    return exchange_cfg.all_symbols_stype_in if all_symbols else exchange_cfg.stype_in
    return "raw_symbol"


def resolve_symbols(
    venue: str,
    all_symbols: bool = False,
    symbols_file: Optional[str] = None,
) -> list[str]:
    """Resolve symbol list for a venue.

    Precedence: an explicit --symbols-file wins, then all_symbols, then the
    venue's basket CSV. The file has to outrank all_symbols because XCME now
    defaults all_symbols on -- checking the flag first would make
    `xcme --symbols-file x.txt` silently download the whole universe instead.
    """
    # Use explicit symbols file if provided
    if symbols_file:
        path = Path(symbols_file)
        if not path.exists():
            raise FileNotFoundError(f"Symbols file not found: {path}")
        with open(path) as f:
            symbols = [line.strip() for line in f if line.strip()]
    elif all_symbols:
        return [ALL_SYMBOLS_SENTINEL]
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
    complete session only -- required for OPRA, see the
    hist_pin_latest_session notes above VENUE_CONFIGS.

    Otherwise: end = asOf+1day (exclusive
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
        known = ", ".join(sorted(VENUE_CONFIGS)) or "(none)"
        raise ValueError(
            f"Unknown venue: {venue}. Venues come from config.ini "
            f"[EXCHANGE:<CODE>] sections with feed=databento; configured: {known}"
        )

    venue_cfg = VENUE_CONFIGS[venue]
    cfg = config.load_databento()

    # Select API key based on venue
    api_key = cfg.keys.get(venue_cfg.venue_name, "")
    if not api_key:
        raise ValueError(
            f"No Databento API key configured for {venue} ({venue_cfg.venue_name}); "
            f"set key_{venue_cfg.venue_name} in conf/keys.ini"
        )

    # Resolve symbols. An explicit --symbols-file turns all_symbols off for the
    # rest of this function: XCME defaults the flag on, and every decision below
    # keys off it, so leaving it set would resolve the file and then ignore it --
    # the definition path downloads the whole universe regardless of `symbols`.
    all_symbols = opts.all_symbols and not opts.symbols_file
    symbols = resolve_symbols(
        venue,
        all_symbols=all_symbols,
        symbols_file=opts.symbols_file,
    )
    stype_in = opts.stype_in or default_stype_in(venue, all_symbols)

    # --all-symbols means the definition schema, for every venue. symbology.resolve
    # stays the route for basket downloads, where the symbol list is explicit and
    # instrument_class is not on offer either way.
    use_definitions = all_symbols and mode == "hist"
    if use_definitions:
        # The definition path writes record.raw_symbol into stype_in_symbol, so the
        # stype_in column has to say raw_symbol or the CSV mislabels its own contents
        # (xcbo would otherwise carry the "parent" default). ALL_SYMBOLS bypasses
        # symbol resolution anyway -- raw_symbol and parent return identical records.
        stype_in = "raw_symbol"

    if opts.dry_run:
        route = "definition schema (batch)" if use_definitions else "symbology.resolve"
        print(f"DRY RUN: Would download {venue} {mode} via {route} "
              f"stype_in={stype_in} for symbols: {symbols}")
        return

    if use_definitions:
        # No CSV staging for this path at all -- the batch job's .dbn.zst lands
        # directly in the venue's manual-drop directory. See
        # _download_definitions_via_batch for why this is a submit+poll+download
        # rather than the streaming approach every other branch here uses.
        dest_dir = paths.manual_venue_dir(opts.date_dir, venue_cfg.venue_name)
        if dest_dir.is_dir() and any(p.name.endswith((".dbn", ".dbn.zst")) for p in dest_dir.iterdir()):
            # An existing file is either an operator's manual drop or a prior
            # successful run of this same branch -- either way it is a complete
            # file (this function only ever moves a fully-downloaded file into
            # place), so re-submitting a job to overwrite it is only waste.
            print(f"  {dest_dir} already has a definition file -- skipping "
                  f"(remove it first to force a re-fetch)")
            return
        client = db.Historical(key=api_key)
        total_bytes = _download_definitions_via_batch(client, venue_cfg, stype_in, opts.date_dir, dest_dir)
        print(f"Wrote {total_bytes:,} byte(s) to {dest_dir}")
        return

    raw_dir = paths.raw_dir(opts.date_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    output_csv = paths.databento_raw_csv(opts.date_dir, venue)
    # PID-scoped so two runs of the same venue cannot share a staging file. A
    # fixed ".tmp.csv" made concurrent runs fight over one path: whichever
    # finished first renamed it away, and the other died at its own rename with
    # "No such file or directory" after resolving the whole basket.
    # Not ".tmp.<pid>.csv": a staging file that still ends in .csv is indistinguishable
    # from a finished venue file to anything globbing the directory, so a killed run
    # leaves something downstream will happily read.
    temp_csv = output_csv.with_name(f"{output_csv.name}.tmp.{os.getpid()}")

    if mode == "hist":
        # use_definitions returned above; every hist download reaching here goes
        # through symbology.resolve.
        client = db.Historical(key=api_key)
        batches = _iter_hist_batches(
            client, venue_cfg, symbols, stype_in, opts.date_dir,
            venue_cfg.hist_lookback_days, opts.hist_range,
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
            # restval="" so the resolve path, which cannot fill instrument_class,
            # writes it empty instead of raising on the missing key. The definition
            # path never reaches here (see the early return above), so this is
            # always MAPPING_COLUMNS now.
            writer = csv.DictWriter(fh, fieldnames=MAPPING_COLUMNS, extrasaction="ignore", restval="")
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
        paths.promote_staging(temp_csv, output_csv)
        print(f"Wrote {total} rows to {output_csv}")
    else:
        # A header-only file would look like a valid empty basket downstream.
        temp_csv.unlink(missing_ok=True)
        print(f"No data retrieved for {venue} {mode}")


def _iter_hist_batches(
    client: db.Historical,
    venue_cfg: config.ExchangeCfg,
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
    venue_cfg: config.ExchangeCfg,
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


def _parse_metadata_ts(raw: str) -> dt.datetime:
    """Databento metadata timestamp -> aware UTC datetime.

    The API sends nanosecond precision ("2026-08-25T11:40:00.000000000Z"),
    which fromisoformat() will not take: it accepts 3 or 6 fractional digits,
    not 9. Truncate the fraction to microseconds and swap Z for +00:00.
    """
    s = raw.replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return dt.datetime.fromisoformat(s)


def _available_end(client: db.Historical, dataset: str, schema: str) -> dt.datetime:
    """Exclusive end of what `dataset` actually holds for `schema`.

    Prefers the per-schema range over the dataset-wide one: they diverge (on
    OPRA.PILLAR, ohlcv-1d ends at 00:00 while definition runs to the current
    minute), and a query is validated against its own schema's range.
    """
    rng = client.metadata.get_dataset_range(dataset=dataset)
    per_schema = (rng.get("schema") or {}).get(schema) or {}
    return _parse_metadata_ts(per_schema.get("end") or rng["end"])


# How far back _prior_session_count will walk looking for a session with data,
# in calendar days. Covers a long weekend plus adjacent holidays; past that,
# treat the absence as unknown rather than as a reason to block the download.
PRIOR_SESSION_LOOKBACK_DAYS = 5


def _offset_into_day(
    midnight: dt.datetime, hhmm: str, fallback: Optional[dt.datetime] = None,
) -> dt.datetime:
    """"HH:MM" as an absolute instant on midnight's UTC day.

    Blank means "no opinion" and returns `fallback` (or midnight itself), which
    is what lets an unset batch_start_time/batch_end_time fall through to the
    plain day boundaries.
    """
    if not hhmm:
        return fallback if fallback is not None else midnight
    hour, _, minute = hhmm.partition(":")
    return midnight + dt.timedelta(hours=int(hour), minutes=int(minute or 0))


def _definition_count(
    client: db.Historical, dataset: str, schema: str, stype_in: str,
    start: str, end: str,
) -> int:
    """Records the definition query would return, without running it.

    metadata.get_record_count is not billed and answers in about a second,
    which is what makes the readiness check below affordable ahead of a job
    that takes ~27 minutes and does bill.
    """
    return client.metadata.get_record_count(
        dataset=dataset,
        symbols=ALL_SYMBOLS_SENTINEL,
        schema=schema,
        stype_in=stype_in,
        start=start,
        end=end,
    )


def _prior_session_count(
    client: db.Historical, dataset: str, schema: str, stype_in: str,
    before: dt.date,
) -> tuple[Optional[dt.date], int]:
    """Definition count for the most recent full day before `before` that has one.

    Walks back a day at a time so weekends and holidays are skipped without a
    calendar dependency -- a non-session simply counts zero. Returns
    (None, 0) if nothing in the window has data, which the caller reads as
    "no baseline" and lets the download through rather than blocking on it.
    """
    for back in range(1, PRIOR_SESSION_LOOKBACK_DAYS + 1):
        day = before - dt.timedelta(days=back)
        try:
            n = _definition_count(
                client, dataset, schema, stype_in,
                day.isoformat(), (day + dt.timedelta(days=1)).isoformat(),
            )
        except Exception:
            # A rejected range (before the dataset starts, say) is not a
            # readiness signal; keep walking.
            continue
        if n > 0:
            return day, n
    return None, 0


# Poll interval/ceiling for the definitions batch job below. Measured on a real
# GLBX.MDP3 ALL_SYMBOLS single-day job (2026-08-24): ~27 minutes queued+processing
# for 1.51M records / 47.3 MB compressed. The ceiling has headroom above that,
# not a tight bound on the observed time -- an automated run should not hang
# forever behind a queue, but 20 minutes false-failed on real, unstuck jobs.
DEFINITION_BATCH_POLL_INTERVAL_SEC = 10
DEFINITION_BATCH_POLL_CEILING_SEC = 45 * 60


def _download_definitions_via_batch(
    client: db.Historical,
    venue_cfg: config.ExchangeCfg,
    stype_in: str,
    date_dir: str,
    dest_dir: Path,
) -> int:
    """ALL_SYMBOLS `definition` schema for one day, via the batch API.

    Replaces the old timeseries.get_range() streaming approach: that read DBN
    records off an HTTP stream and re-encoded them as CSV text by hand, which
    is strictly lossier and slower than just keeping what Databento already
    sends -- a DBN file, zstd-compressed. This submits a batch job for the
    same query and downloads the resulting .dbn.zst straight into dest_dir
    with no CSV in between. normalize/databento_norm.py reads a venue's
    manual-drop directory (paths.manual_venue_dir) in preference to a streamed
    CSV, and that is exactly what dest_dir is -- an operator's own manual
    batch download and this automated one land in the same place and are
    indistinguishable to normalize.

    The requested window is date_dir's single UTC day, with `end` clamped to
    the dataset's actual available end. Intraday, date_dir+1day is tomorrow
    midnight UTC and the API rejects it with 422 data_end_after_available_end
    ("OPRA.PILLAR has data available up to 2026-08-25 11:40"), which made
    every same-day OPRA run fail outright.

    Omitting `end` does not help and was tried: a date-only `start` with no
    `end` is forward-filled by the server to start+1day -- the identical 422 --
    and a datetime `start` with no `end` is refused with 422
    data_start_too_precise_to_forward_fill. The range has to be closed, so it
    is closed here against metadata.get_dataset_range(). Clamping also keeps a
    backfill (--date-dir in the past) to its one day instead of letting it run
    to now.

    A day that has no data at all yet raises rather than submitting an empty
    or inverted range: the matching engine may not have produced the session
    when an automated run fires, and silently substituting a different day --
    what the old streaming path did -- would hide exactly that condition.

    Polling failures are the other case that must not go quiet: a job stuck
    past DEFINITION_BATCH_POLL_CEILING_SEC raises rather than looping forever,
    since this runs inside an automated pipeline step, not a script someone is
    watching.
    """
    # The window comes from metadata.get_dataset_range(), not from date_dir.
    #
    # Asking for date_dir and hoping it exists is what kept failing: the venues
    # publish their definition snapshot at different hours -- GLBX in the
    # 00:00-01:00Z hour, EQUS ~05:00-06:00Z, OPRA ~10:00-11:00Z -- so any run
    # before a venue's hour asked for a day that dataset did not have yet and
    # blocked. Deriving the day from the dataset's own available range instead
    # means the request is always for a session that exists.
    #
    # dataset_range["end"] is EXCLUSIVE (resolve_hist_range documents the same
    # thing: 2026-08-03 being the last session with data was reported as
    # end=2026-08-04T00:00:00Z), so the last session carrying data is the day
    # containing end minus an instant -- not end's own date.
    available_end = _available_end(client, venue_cfg.dataset, venue_cfg.schema)
    as_of = (available_end - dt.timedelta(microseconds=1)).date()
    start = as_of - dt.timedelta(days=venue_cfg.batch_lookback_days)
    midnight = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    day_end = dt.datetime.combine(as_of, dt.time.min, tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    if as_of.strftime("%Y%m%d") != date_dir:
        # date_dir still decides where the file lands; the data inside it is
        # whatever session the dataset actually has. Said out loud because the
        # two can now disagree -- a 03:00Z run writes 20260827/XCBO/ holding
        # 08-26's OPRA snapshot, which is correct but worth not discovering
        # from a filename.
        print(f"  {venue_cfg.dataset}: latest session with data is "
              f"{as_of.isoformat()}, writing under date_dir {date_dir}", flush=True)

    # [EXCHANGE:<CODE>] batch_start_time/batch_end_time narrow the day; they can
    # never widen it. max()/min() against the plain day is what enforces that,
    # so a stale or over-eager value in config.ini costs coverage rather than
    # producing a range the API will reject.
    start_ts = max(midnight, _offset_into_day(midnight, venue_cfg.batch_start_time))
    if start_ts != midnight:
        # Databento serves `definition` as a daily snapshot anchored to UTC
        # midnight; its own client warns that "instrument definitions effective
        # on this date may be missing" when a request starts later. Measured on
        # 2026-08-21, a 13:30Z start returned 90 OPRA records against 2,253,273
        # for the day. Loud rather than fatal: the knob is config, and an
        # operator narrowing on purpose should not be blocked by this.
        print(f"  WARNING: batch_start_time={venue_cfg.batch_start_time} is not "
              f"00:00 UTC. The definition snapshot is anchored to UTC midnight, "
              f"so this will silently drop definitions effective on "
              f"{start.isoformat()}. Pin batch_start_time = 00:00 unless you "
              f"know exactly why you are not.", flush=True)
    configured_end = min(day_end, _offset_into_day(midnight, venue_cfg.batch_end_time, day_end))
    end = min(configured_end, available_end)
    if end <= start_ts:
        raise RuntimeError(
            f"{venue_cfg.dataset} has no definition data in "
            f"{start.isoformat()}..{as_of.isoformat()} yet "
            f"(available up to {available_end.isoformat()}) -- nothing to download"
        )

    have = _definition_count(client, venue_cfg.dataset, venue_cfg.schema,
                             stype_in, start_ts.isoformat(), end.isoformat())
    prior_day, prior = _prior_session_count(
        client, venue_cfg.dataset, venue_cfg.schema, stype_in, start)  # start = window open
    floor = int(prior * venue_cfg.definition_ready_ratio)
    if prior and have < floor:
        raise RuntimeError(
            f"{venue_cfg.dataset} definitions for "
            f"{start.isoformat()}..{as_of.isoformat()} are not "
            f"published yet: {have:,} records up to {end.isoformat()} against "
            f"{prior:,} on {prior_day.isoformat()} "
            f"({have / prior:.1%}, floor {venue_cfg.definition_ready_ratio:.0%}). "
            f"Downloading now would write a file holding a fraction of the "
            f"session. Re-run once they land -- OPRA publishes ~06:00-07:00 ET, "
            f"GLBX before 04:00 ET."
        )

    clamped = " (clamped to available)" if end < day_end else ""
    baseline = (f", {have:,} records vs {prior:,} on {prior_day.isoformat()}"
                if prior else f", {have:,} records (no baseline)")
    print(f"  Submitting batch job: {venue_cfg.dataset} ALL_SYMBOLS definition, "
          f"{start_ts.isoformat()}..{end.isoformat()}{clamped}{baseline}, "
          f"encoding=dbn compression=zstd", flush=True)
    ack = client.batch.submit_job(
        dataset=venue_cfg.dataset,
        symbols=ALL_SYMBOLS_SENTINEL,
        schema=venue_cfg.schema,
        stype_in=stype_in,
        start=start_ts.isoformat(),
        end=end.isoformat(),
        encoding="dbn",
        compression="zstd",
        split_duration="day",
        delivery="download",
    )
    job_id = ack["id"]
    state = ack.get("state", "")
    print(f"    job {job_id}: {state}", flush=True)

    elapsed = 0
    while state not in ("done", "expired"):
        if elapsed >= DEFINITION_BATCH_POLL_CEILING_SEC:
            raise RuntimeError(
                f"batch job {job_id} still {state!r} after {elapsed}s -- giving up. "
                f"The job itself is unaffected and can be downloaded later with "
                f"client.batch.download({job_id!r}, ...) once it finishes."
            )
        time.sleep(DEFINITION_BATCH_POLL_INTERVAL_SEC)
        elapsed += DEFINITION_BATCH_POLL_INTERVAL_SEC
        state = client.batch.get_job_details(job_id).get("state", "")
        print(f"    job {job_id}: {state} ({elapsed}s)", flush=True)

    if state == "expired":
        raise RuntimeError(f"batch job {job_id} expired before it could be downloaded")

    files = client.batch.list_files(job_id)
    data_files = sorted(
        str(f["filename"]) for f in files
        if str(f.get("filename", "")).endswith((".dbn", ".dbn.zst"))
    )
    if not data_files:
        raise RuntimeError(f"batch job {job_id} finished with no .dbn/.dbn.zst file")

    # batch.download() nests under {output_dir}/{job_id}/{filename}; the date
    # dir is passed as output_dir so that lands as {date_dir}/{job_id}/{name},
    # then each file is moved up into dest_dir (…/{VENUE}/{name}) to match the
    # flat layout a manual extraction produces -- normalize's manual-drop
    # reader globs dest_dir directly, one level, no job-id subfolder.
    dest_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for name in data_files:
        written = client.batch.download(job_id=job_id, output_dir=dest_dir.parent, filename_to_download=name)
        for src in written:
            target = dest_dir / src.name
            os.replace(src, target)
            total_bytes += target.stat().st_size
            print(f"    {target}", flush=True)
        job_scratch = dest_dir.parent / job_id
        if job_scratch.is_dir() and not any(job_scratch.iterdir()):
            job_scratch.rmdir()
    return total_bytes


def _iter_live_batches(
    api_key: str,
    venue_cfg: config.ExchangeCfg,
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
    venue_cfg: config.ExchangeCfg,
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
