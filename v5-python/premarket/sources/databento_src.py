"""Databento market data integration (wraps official databento SDK).

Matches v4-golang's internal/databento exactly:
  - dataset names: GLBX.MDP3 (XCME), OPRA.PILLAR (XCBO), EQUS.MINI (XNAS)
  - stype_in defaults: XCME=parent (raw_symbol if --all-symbols), XCBO=parent, XNAS=raw_symbol
  - stype_out sent to API is always instrument_id
  - date range computed from metadata.get_dataset_range() minus a lookback window
  - output: YYYYMMDD/raw/{VENUE}-DATABENTO.csv with columns matching
    internal/databento/mapping.go's MappingColumns
"""
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import databento as db
import pandas as pd

from .. import config, paths, runner

ALL_SYMBOLS_SENTINEL = "ALL_SYMBOLS"

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
    uses_es_key: bool = False


VENUE_CONFIGS = {
    "xcme": VenueConfig(venue_name="XCME", dataset="GLBX.MDP3", uses_es_key=True),
    "xcbo": VenueConfig(venue_name="XCBO", dataset="OPRA.PILLAR", uses_es_key=False),
    "xnas": VenueConfig(venue_name="XNAS", dataset="EQUS.MINI", uses_es_key=False),
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


def resolve_hist_range(client: db.Historical, dataset: str, as_of: str, lookback_days: int) -> tuple[str, str]:
    """
    Compute (start_date, end_date) for the hist resolve request.
    Matches ResolveHistRange in v4-golang: end = asOf+1day (exclusive UTC midnight,
    clamped to dataset's actual available end), start = end - lookback_days
    (clamped to dataset's actual available start).
    """
    dataset_range = client.metadata.get_dataset_range(dataset=dataset)
    first = dt.datetime.strptime(dataset_range["start"][:10], "%Y-%m-%d").date()
    last = dt.datetime.strptime(dataset_range["end"][:10], "%Y-%m-%d").date()

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
    api_key = cfg.api_key_es if venue_cfg.uses_es_key else cfg.api_key
    if not api_key:
        raise ValueError(f"No Databento API key configured for {venue}")

    client = db.Historical(key=api_key)

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
    temp_csv = output_csv.with_suffix(".tmp.csv")

    try:
        if mode == "hist":
            rows = _fetch_hist(client, venue_cfg, symbols, stype_in, opts.date_dir, cfg.hist_lookback_days)
        elif mode == "live":
            raise NotImplementedError("Live mode not yet implemented")
        else:
            raise ValueError(f"Unknown mode: {mode}")

        if rows:
            df = pd.DataFrame(rows, columns=MAPPING_COLUMNS)
            df.to_csv(temp_csv, index=False, encoding="utf-8-sig")
            temp_csv.rename(output_csv)
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
) -> list[dict]:
    """Single batch symbology.resolve() call, matching HistoricalSymbolMappings in v4-golang."""
    start_date, end_date = resolve_hist_range(client, venue_cfg.dataset, as_of, lookback_days)

    print(f"  Resolving {venue_cfg.venue_name} hist: {len(symbols)} symbol(s), "
          f"stype_in={stype_in}, range=[{start_date}, {end_date})")

    result = client.symbology.resolve(
        dataset=venue_cfg.dataset,
        symbols=symbols,
        stype_in=stype_in,
        stype_out="instrument_id",
        start_date=start_date,
        end_date=end_date,
    )

    rows = []
    mappings = result.get("result", {})
    for stype_in_symbol, entries in mappings.items():
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

    not_found = result.get("not_found", [])
    if not_found:
        print(f"    Warning: not found: {not_found}")

    return rows
