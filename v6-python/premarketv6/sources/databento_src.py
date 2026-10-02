"""Databento market data integration (wraps official databento SDK).

Venue wiring:
  - dataset names: GLBX.MDP3 (XCME), OPRA.PILLAR (XCBO), EQUS.MINI (XNAS)
  - the venue table itself is config.ini's [EXCHANGE:<CODE>] sections, not code:
    dataset, stype_in, schema, --all-symbols default and the clamp/readiness
    knobs all live there (premarketv6.config.load_exchanges)
  - stype_in defaults: XCME=parent (raw_symbol if --all-symbols), XCBO=parent, XNAS=raw_symbol
  - --all-symbols (on by default) and an explicit --symbols-file both mean the
    definition schema from a LIVE replay (_download_definitions_live), landing
    as .dbn.zst; the historical batch route is gone, because Historical and Live
    assign instrument_id independently and the MDF lanes run on live ids
  - symbology.resolve is still the route for the basket CSV (--no-all-symbols),
    which cannot carry instrument_class
  - stype_out sent to API is always instrument_id
  - date range computed from metadata.get_dataset_range() minus a lookback window
  - output:
      - definition schema (any venue, live replay): YYYYMMDD/{VENUE}/*.dbn.zst,
        via paths.manual_venue_dir -- no CSV, read directly by normalize
      - basket downloads: YYYYMMDD/raw/{VENUE}-DATABENTO.csv, columns matching
        internal/databento/mapping.go's MappingColumns
"""
import csv
import datetime as dt
import os
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
# schema (today: the live replay) for every venue, so all three land as .dbn.zst.
#
# The cost of the carve-out was instrument_class. symbology.resolve returns ids
# and dates and nothing else, so XNAS shipped 13,195 rows with the column blank
# and the normalizer fell back to parsing symbol strings, while XCBO and XCME
# read it straight off the InstrumentDefMsg records.

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
    if not venue_cfg.enabled:
        # Refused rather than skipped: naming a venue on the command line is an
        # explicit request, and returning quietly would look like a download
        # that found nothing rather than one that never ran.
        raise ValueError(
            f"{venue} ({venue_cfg.venue_name}) is disabled: set enabled = 1 in "
            f"config.ini [EXCHANGE:{venue_cfg.venue_name}] to download it"
        )
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

    # --all-symbols means the definition schema, for every venue, and so does an
    # explicit --symbols-file: the job then carries the file's symbols. The
    # resolve call below passes no end_date, so the API answers for the start
    # day alone -- a --symbols-file run on 2026-09-28 came back with the
    # contracts of 2026-09-22 (XCME, XNAS) and 2026-09-25 (XCBO). symbology.resolve
    # stays the route for the basket CSV only.
    # Definitions come from the live API only, whatever --hist/--live says: the
    # historical batch route is gone (see "definitions: the live replay" below).
    use_definitions = all_symbols or bool(opts.symbols_file)
    if use_definitions and all_symbols:
        # The definition path writes record.raw_symbol into stype_in_symbol, so the
        # stype_in column has to say raw_symbol or the CSV mislabels its own contents
        # (xcbo would otherwise carry the "parent" default). ALL_SYMBOLS bypasses
        # symbol resolution anyway -- raw_symbol and parent return identical records.
        stype_in = "raw_symbol"

    if opts.dry_run:
        route = "definition schema (live replay)" if use_definitions else "symbology.resolve"
        print(f"DRY RUN: Would download {venue} {mode} via {route} "
              f"stype_in={stype_in} for symbols: {symbols}")
        return

    if use_definitions:
        # No CSV staging for this path at all -- the live replay is written as one
        # .dbn.zst straight into the venue's manual-drop directory, where
        # normalize reads it.
        dest_dir = paths.manual_venue_dir(opts.date_dir, venue_cfg.venue_name)
        if dest_dir.is_dir() and any(p.name.endswith((".dbn", ".dbn.zst")) for p in dest_dir.iterdir()):
            # An existing file is either an operator's manual drop or a prior
            # successful run of this same branch -- either way it is a complete
            # file (this function only ever moves a fully-downloaded file into
            # place), so re-submitting a job to overwrite it is only waste.
            print(f"  {dest_dir} already has a definition file -- skipping "
                  f"(remove it first to force a re-fetch)")
            return
        total_bytes = _download_definitions_live(
            api_key, venue_cfg, stype_in, opts.date_dir, dest_dir,
            ALL_SYMBOLS_SENTINEL if all_symbols else symbols)
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


# --- definitions: the live replay ---------------------------------------------
#
# Every definition file comes from the LIVE API. Databento assigns instrument_id
# independently in Historical and Live, for every dataset ("you should not assume
# the instrument ID assignment across the Historical and Live APIs are
# consistent", Databento support, 2026-10-02), and the token map the MDF lanes
# run on is keyed by instrument_id -- so the ids have to be the live session's.
# The historical batch route this replaced was wrong for OPRA in a way nothing
# downstream could see: measured 2026-10-02 on the 8 XCBO parents, 501 of 55,052
# live ids matched the batch file, 29,545 named a DIFFERENT contract in it, and
# the lane sent those contracts' prices under the wrong tokens.
#
# What a live definition subscription replays (start=0, measured 2026-09-27..
# 2026-10-02):
#
#   GLBX.MDP3    session starts Sun ~14:30Z with every outright; after that only
#                new spreads, all day (mostly user-defined, which expire at the
#                21:00Z close of the day they were made). No daily re-send.
#   EQUS.MINI    session starts Mon ~05:00Z; every definition re-sent daily
#                ~05:00Z with the same ids.
#   OPRA.PILLAR  session starts Mon 10:30Z; every definition re-sent daily 10:30Z
#                with the same ids, new listings daily ~12:00Z.
#
# Live ids did not change within that week for any of the three. Whether they
# reset at the weekly gateway restart is not established; a daily fetch after
# the session start picks up whatever the session uses either way.

# Cap on waiting for "Finished definition replay". OPRA's 8-parent replay took
# 58 s on 2026-10-02 (247,646 records for 55,052 instruments).
LIVE_DEFINITION_REPLAY_CEILING_SEC = 15 * 60

# Datasets that re-send every definition once a day. A replay with nothing from
# the trade date on one of these was taken before that re-send, which means it
# describes yesterday -- refused rather than written under today's date.
LIVE_DAILY_RESEND = {"OPRA", "EQUS"}

# Datasets that list new contracts at a known time of day (UTC hour, minute). A
# fetch before it is complete for everything listed so far but misses the new
# listings, so it warns rather than refuses.
LIVE_NEW_LISTINGS_UTC = {"OPRA": (12, 0)}

_UNDEF_TIMESTAMP = 2**64 - 1  # databento_dbn.UNDEF_TIMESTAMP: no expiration


def _dataset_key(dataset: str) -> str:
    """'OPRA.PILLAR' -> 'OPRA'."""
    return dataset.split(".", 1)[0].strip().upper()


def _day_start_ns(date_dir: str) -> int:
    """YYYYMMDD -> that day's 00:00 UTC in Unix nanoseconds."""
    day = dt.datetime.strptime(date_dir, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    return int(day.timestamp()) * 1_000_000_000


def select_live_definitions(records, day_start_ns: int):
    """The trade date's instruments from a live definition replay.

    The replay carries every definition the session has sent since it started,
    re-sends included, so one instrument appears up to once a day. The last copy
    wins (the replay is in send order). Anything that expired before the trade
    date is dropped: the session still holds the week's expired options and
    yesterday's user-defined spreads, which are not tradable today.

    Measured against the historical batch for 2026-10-02: identical contract sets
    for OPRA (50,244) and EQUS (8); for GLBX the batch also carried 1,171
    user-defined spreads that had expired on earlier days, which this drops.

    Returns (kept, latest_count), kept sorted by instrument_id.
    """
    latest = {}
    for record in records:
        latest[record.instrument_id] = record
    kept = [r for r in latest.values()
            if r.expiration == _UNDEF_TIMESTAMP or r.expiration >= day_start_ns]
    kept.sort(key=lambda r: r.instrument_id)
    return kept, len(latest)


def encode_definition_file(records, dataset: str, stype_in: str, symbols,
                           start_ns: int, end_ns: int, version: int) -> bytes:
    """A zstd-compressed DBN definition file holding exactly `records`.

    The live stream's own metadata is not usable as a file header: a live session
    reports schema and stype_in as None (it may mix schemas), and normalize only
    reads files whose metadata says definition and the venue's dataset. So the
    header is written here, at the DBN version the records were sent in, and the
    file is indistinguishable to normalize from the batch download it replaced.
    """
    import databento_dbn as dbn
    import zstandard

    metadata = dbn.Metadata(
        dataset=dataset,
        start=start_ns,
        stype_in=dbn.SType(stype_in),
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.DEFINITION,
        symbols=list(symbols),
        end=end_ns,
        version=version,
    )
    raw = bytes(metadata.encode()) + b"".join(bytes(r) for r in records)
    return zstandard.ZstdCompressor().compress(raw)


def _download_definitions_live(
    api_key: str,
    venue_cfg: config.ExchangeCfg,
    stype_in: str,
    date_dir: str,
    dest_dir: Path,
    symbols=ALL_SYMBOLS_SENTINEL,
    live_factory=None,
    now: Optional[dt.datetime] = None,
) -> int:
    """`definition` for the trade date from a live replay, as one .dbn.zst file.

    Subscribes with start=0, which replays every definition since the session
    started, waits for Databento's "Finished definition replay", keeps the trade
    date's instruments (select_live_definitions) and writes them to dest_dir
    under the name the batch download used. Returns bytes written.

    Only the current session can be replayed, so date_dir must be today (UTC).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if date_dir != now.strftime("%Y%m%d"):
        raise ValueError(
            f"live definitions describe the current session only: --date-dir "
            f"{date_dir} is not today ({now.strftime('%Y%m%d')} UTC)")

    key = _dataset_key(venue_cfg.dataset)
    listing = LIVE_NEW_LISTINGS_UTC.get(key)
    if listing and (now.hour, now.minute) < listing:
        print(f"  WARNING: {venue_cfg.dataset} lists new contracts at ~{listing[0]:02d}:"
              f"{listing[1]:02d}Z and it is {now.strftime('%H:%M')}Z -- today's new "
              f"listings will be missing from this file")

    sym_list = [symbols] if isinstance(symbols, str) else list(symbols)
    print(f"  Live definition replay: {venue_cfg.dataset} stype_in={stype_in} "
          f"symbols={sym_list}", flush=True)
    client = (live_factory or db.Live)(key=api_key)
    client.subscribe(dataset=venue_cfg.dataset, schema="definition",
                     stype_in=stype_in, symbols=sym_list, start=0)
    definitions, version, finished = [], None, False
    deadline = time.monotonic() + LIVE_DEFINITION_REPLAY_CEILING_SEC
    try:
        for record in client:
            if version is None and getattr(client, "metadata", None) is not None:
                version = client.metadata.version
            if isinstance(record, db.InstrumentDefMsg):
                definitions.append(record)
            elif isinstance(record, db.ErrorMsg):
                raise RuntimeError(f"{venue_cfg.dataset} live replay error: {record.err}")
            elif isinstance(record, db.SystemMsg) and "Finished definition replay" in record.msg:
                finished = True
                break
            if time.monotonic() > deadline:
                break
    finally:
        try:
            client.stop()
        except Exception:
            pass  # a stop after a completed replay may report the session's warnings
    if not finished:
        raise RuntimeError(
            f"{venue_cfg.dataset}: no 'Finished definition replay' within "
            f"{LIVE_DEFINITION_REPLAY_CEILING_SEC}s ({len(definitions):,} definitions so far)")

    day_start = _day_start_ns(date_dir)
    kept, distinct = select_live_definitions(definitions, day_start)
    if not kept:
        raise RuntimeError(f"{venue_cfg.dataset}: the live replay holds no instrument "
                           f"for {date_dir} ({distinct:,} distinct, all expired)")
    refreshed = sum(1 for r in kept if r.ts_recv >= day_start)
    if key in LIVE_DAILY_RESEND and refreshed == 0:
        raise RuntimeError(
            f"{venue_cfg.dataset}: nothing in the live replay was sent on {date_dir} -- "
            f"today's daily re-send has not happened yet (see the runbook for times)")

    newest = max(r.ts_recv for r in kept)
    print(f"  {len(definitions):,} definition record(s), {distinct:,} instrument(s); "
          f"kept {len(kept):,} for {date_dir}, dropped {distinct - len(kept):,} expired; "
          f"{refreshed:,} sent today, newest "
          f"{dt.datetime.fromtimestamp(newest / 1e9, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z",
          flush=True)

    blob = encode_definition_file(
        kept, venue_cfg.dataset, stype_in, sym_list, day_start,
        int(now.timestamp() * 1e9), version or 3)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{venue_cfg.dataset.lower().replace('.', '-')}-{date_dir}.definition.dbn.zst"
    staging = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    staging.write_bytes(blob)
    os.replace(staging, target)
    print(f"    {target}", flush=True)
    return len(blob)


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
