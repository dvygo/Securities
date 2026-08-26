"""Fyers normalization: map raw rows to 16-column canonical schema."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional


from .. import config, parquet_export, paths, runner
from ..sources import fyers_src
from . import broker_script, counter_token, price, session


# Broad category for scriptInstrumentType2.
def instrument_type2(inst_type: str) -> str:
    t = (inst_type or "").upper()
    if t == "EQ":
        return "EQUITY"
    if t.startswith("FUT"):
        return "FUTURE"
    if t.startswith("OPT"):
        return "OPTION"
    return t


def classify_instrument(ex_inst_type: str) -> str:
    """exInstType appendix code -> instrument type name (EQ, FUTIDX, OPTSTK, ...).
    Unknown codes fall back to UNKNOWN_<code>."""
    code = (ex_inst_type or "").strip()
    name = fyers_src.INSTRUMENT_CODES.get(_safe_int(code))
    if name:
        return name
    return f"UNKNOWN_{code}" if code else "UNKNOWN"


def _safe_int(raw: str) -> Optional[int]:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _expiration_ns(raw: str) -> int:
    """Convert a raw expiryDate field (Unix seconds, ms, or YYYYMMDD) to nanoseconds UTC."""
    s = (raw or "").strip()
    if not s or s in ("0", "-1"):
        return 0
    try:
        ts = int(float(s))
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)
            return int(dt.timestamp()) * 10**9
        except ValueError:
            return 0
    if ts <= 0:
        return 0
    if ts < 1_000_000_000_000:
        return ts * 1_000_000_000
    if ts < 1_000_000_000_000_000:
        return ts * 1_000_000
    return ts


def map_fyers_row(row: Dict[str, str], cfg: config.NormalizerCfg) -> Dict[str, Any]:
    """
    Map a single Fyers raw row to the 16-column canonical schema.
    Field keys here match the real feed, e.g.
    row["symTicker"] is the ticker, row["symDetails"] is the description --
    NOT "symbol"/"description", which never existed on the actual wire.
    Returns dict with canonical columns (may be sparse).
    """
    result = {}

    # Basic identifiers
    result["script"] = row.get("symTicker", "")
    result["scriptToken"] = row.get("exToken", "")
    result["scriptDetails"] = row.get("symDetails", "") or result["script"]

    # Exchange/MIC resolution
    exchange = row.get("exchange", "")
    segment = row.get("segment", "")
    result["exchange"] = fyers_src.resolve_exchange_mic(exchange, segment)

    # Instrument type classification
    inst_type = classify_instrument(row.get("exInstType", ""))
    result["scriptInstrumentType"] = inst_type
    result["scriptInstrumentType2"] = instrument_type2(inst_type)

    opt_type = (row.get("optType", "") or "").strip().upper()
    result["optionType"] = "CALL" if opt_type == fyers_src.OPTION_TYPE_CE else (
        "PUT" if opt_type == fyers_src.OPTION_TYPE_PE else ""
    )

    # ISIN
    result["ISIN"] = row.get("isin", "")

    # Price fields (scaled). multiplier = wire price scale (matches US
    # convention): feed is quoted in paise, so strike/tickSize/multiplier
    # all use the same 100x scale.
    result["multiplier"] = price.INDIA_PRICE_SCALE
    result["tickSize"] = price.scale_price(row.get("tickSize", "0"))

    strike = row.get("strikePrice", "")
    try:
        strike_valid = strike and float(strike) > 0
    except ValueError:
        strike_valid = False
    result["strike"] = price.scale_price(strike) if strike_valid else 0

    # Quantities
    lot_size = row.get("minLotSize", "1")
    try:
        result["lotSize"] = int(float(lot_size)) if lot_size else 1
    except ValueError:
        result["lotSize"] = 1

    # Currency (default to INR for India)
    result["currency"] = "INR"

    # Underlying: exSymName is the short underlying/company name on the wire.
    underlying = row.get("exSymName", "")
    result["underlying"] = underlying
    result["underlying_root"] = underlying

    # Trading session: real per-row IST session string, e.g.
    # "0915-1530|1815-1915:" -- Fyers does carry this on the wire, but never
    # includes the NSE/BSE pre-open auction window, so prepend it ourselves
    # for cash-market (CM) equities.
    session_utc = session.trading_session_ist_to_utc(row.get("tradingSession", ""))
    is_cm = fyers_src.SEGMENT_CODES.get(_safe_int(segment)) == "CM"
    if is_cm and session_utc:
        session_utc = f"{session.NSE_PREOPEN_UTC}|{session_utc}"
    result["tradingSessionUTC"] = session_utc

    # Expiration (Unix seconds/ms or YYYYMMDD -> nanoseconds UTC)
    result["expiration"] = _expiration_ns(row.get("expiryDate", ""))

    # Broker symbology: no India broker format is defined yet, so brokerScript1
    # takes the same exact-copy-of-script fallback the US venues use for rows
    # they cannot decompose.
    result["brokerScript1"] = broker_script.from_equity(result["script"])
    broker_script.fill_unspecified(result)

    return result


def run(opts: runner.Opts) -> None:
    """Normalize Fyers data: read raw CSVs, map to canonical schema, write normalized CSVs."""
    if opts.dry_run:
        print("DRY RUN: Would normalize Fyers data")
        return

    cfg = config.load_normalizer()
    normalized_dir = paths.normalized_dir(opts.date_dir)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Process each Fyers MIC bundle
    # Same pre-flight the Databento path runs: a bad base is a CRITICAL config
    # error and the MIC is skipped, not numbered into unusable tokens.
    token_errors = counter_token.validate(config.load_exchanges())

    for mic, (output_csv, table_name, source_files) in paths.FYERS_MIC_BUNDLES.items():
        if mic in token_errors:
            for msg in token_errors[mic]:
                print(f"  CRITICAL [{mic}] counterToken config: {msg}")
            print(f"  CRITICAL: skipping Fyers {mic} -- fix conf/config.ini [EXCHANGE:{mic}]")
            continue
        bundle_dir = paths.venue_dir(opts.date_dir, mic)
        if not bundle_dir.is_dir():
            print(f"  No raw directory for Fyers {mic} ({bundle_dir})")
            continue
        print(f"  Normalizing Fyers {mic}...")

        all_rows = []
        for source_file in source_files:
            source_path = bundle_dir / source_file
            if not source_path.exists():
                continue

            raw_rows = fyers_src.parse_fyers_csv(source_path)
            for raw_row in raw_rows:
                norm_row = map_fyers_row(raw_row, cfg)
                # Filter out empty/invalid rows
                if norm_row.get("script") and norm_row.get("exchange"):
                    all_rows.append(norm_row)

        # Number this MIC's rows into its reserved block, after the script/exchange
        # filter above so the sequence has no gaps. One counter per output file,
        # which is what has to be internally unique -- XNSE and XBOM each merge
        # several source feeds into a single file.
        bases = counter_token.bases_for(mic)
        if bases is not None:
            # Row count is known here, so the block is checked before numbering
            # rather than discovered by assign() partway through.
            counter_token.check_capacity(mic, bases[0], len(all_rows))
            for n, row in enumerate(all_rows, 1):
                row["counterToken"] = counter_token.assign(bases, n)

            # counterTokenV2: stable across days. Every row is already in
            # memory here, so the whole symbol set is known and the carry-
            # forward needs no extra pass.
            exchange_cfg = counter_token.exchange_for(mic)
            v2_base = exchange_cfg.counter_base_v2
            try:
                previous, prev_day = counter_token.previous_tokens(
                    opts.date_dir, mic, exchange_cfg.venue_id)
            except ValueError as exc:
                print(f"  CRITICAL: skipping Fyers {mic} -- {exc}")
                continue
            scripts = [r.get("script", "") for r in all_rows]
            tokens = counter_token.carry_forward(
                previous, scripts, exchange_cfg.venue_id, v2_base)
            counter_token.check_capacity(mic, v2_base, tokens.high_water)
            for row in all_rows:
                row["counterTokenV2"] = tokens.token(row.get("script", ""))
            counter_token.merge_into_manifest(opts.date_dir, mic, tokens)
            reused = len(tokens.assigned) - (
                0 if previous is None
                else len(set(tokens.assigned) & set(previous.assigned)))
            print(f"    {mic} counterTokenV2: {len(tokens.assigned):,} symbol(s), "
                  f"{reused:,} new, high-water {tokens.high_water:,}"
                  + (f", carried from {prev_day}" if previous else ", first day"))

        # Write normalized Parquet
        output_path = normalized_dir / output_csv
        if all_rows:
            # RowWriter fills a missing key with "" and orders by the column list,
            # so the frame-shaping the CSV path needed is gone.
            parquet_export.write_rows(output_path, paths.NORMALIZED_COLUMNS, all_rows)
            print(f"    Wrote {len(all_rows)} rows to {output_path}")
        else:
            # Nothing is written for an empty MIC. A schema-only file would look
            # like a valid empty venue to everything downstream; an absent one
            # is skipped by the glob, which is what "no data" should look like.
            print(f"    No rows for {mic}")
