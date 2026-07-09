"""Databento market data integration (wraps official databento SDK)."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import databento as db
import pandas as pd

from .. import config, paths, runner


# Per-venue configuration
@dataclass
class VenueConfig:
    """Databento venue configuration."""
    venue_name: str
    dataset: str
    output_csv: str
    uses_es_key: bool = False
    default_stype_in: str = "raw_symbol"


VENUE_CONFIGS = {
    "xcme": VenueConfig(
        venue_name="XCME",
        dataset="GLBX.MDP3",
        output_csv="databento_xcme.csv",
        uses_es_key=True,
        default_stype_in="raw_symbol",
    ),
    "xcbo": VenueConfig(
        venue_name="XCBO",
        dataset="OPRA.PILLAR",
        output_csv="databento_xcbo.csv",
        uses_es_key=False,
        default_stype_in="raw_symbol",
    ),
    "xnas": VenueConfig(
        venue_name="XNAS",
        dataset="EQUS.MINI",
        output_csv="databento_xnas.csv",
        uses_es_key=False,
        default_stype_in="raw_symbol",
    ),
}


def resolve_symbols(
    venue: str,
    all_symbols: bool = False,
    symbols_file: Optional[str] = None,
) -> list[str]:
    """Resolve symbol list for a venue."""
    if all_symbols:
        return ["ALL_SYMBOLS"]

    if symbols_file and Path(symbols_file).exists():
        with open(symbols_file) as f:
            return [line.strip() for line in f if line.strip()]

    # Default underlyings per venue
    if venue == "xcme":
        return ["ES"]  # E-mini S&P 500
    elif venue == "xcbo":
        # OPRA option roots
        return [".SPX", ".NDX", ".RUT"]
    elif venue == "xnas":
        return ["AAPL", "MSFT", "GOOGL"]  # sample equities

    return []


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

    if opts.dry_run:
        print(f"DRY RUN: Would download {venue} {mode} for symbols: {symbols}")
        return

    # Create output directory
    raw_dir = paths.raw_dir(opts.date_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    output_csv = raw_dir / f"databento_{venue}_{mode}.csv"
    temp_csv = output_csv.with_suffix(".tmp.csv")

    rows = []

    try:
        if mode == "hist":
            # Historical data
            for symbol in symbols:
                print(f"  Downloading {venue} hist: {symbol}")
                data = client.get_range(
                    dataset=venue_cfg.dataset,
                    symbols=symbol,
                    schema="symbology",
                    stype_in=opts.stype_in or venue_cfg.default_stype_in,
                    start=None,  # Let SDK handle date range
                )
                if data is not None and hasattr(data, '__iter__'):
                    for record in data:
                        rows.append(record)

        elif mode == "live":
            # Live streaming data (uses Live client)
            from . import databento_live
            databento_live.fetch_live(venue, venue_cfg, opts, rows)

        # Write to CSV
        if rows:
            df = pd.DataFrame(rows)
            df.to_csv(temp_csv, index=False, encoding="utf-8-sig")
            temp_csv.rename(output_csv)
            print(f"Wrote {len(df)} rows to {output_csv}")
        else:
            print(f"No data retrieved for {venue} {mode}")

    except Exception as e:
        if temp_csv.exists():
            temp_csv.unlink()
        raise


def symbology_hist(venue: str, symbol: str) -> pd.DataFrame:
    """
    Fetch historical symbology mappings for a symbol.
    Returns DataFrame with symbology resolution.
    """
    cfg = config.load_databento()
    venue_cfg = VENUE_CONFIGS.get(venue)
    if not venue_cfg:
        raise ValueError(f"Unknown venue: {venue}")

    api_key = cfg.api_key_es if venue_cfg.uses_es_key else cfg.api_key
    client = db.Historical(key=api_key)

    # Get symbology resolve data
    data = client.symbology_resolve(
        dataset=venue_cfg.dataset,
        symbols=symbol,
        stype_in="raw_symbol",
        stype_out="isin",
    )

    return data


def symbology_live(venue: str, symbol: str) -> pd.DataFrame:
    """
    Fetch live symbology mappings for a symbol.
    Uses live streaming with definition schema.
    """
    # This would use databento.Live client with definition schema
    # For now, placeholder
    pass
