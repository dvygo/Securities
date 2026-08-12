"""Databento normalization: GLBX/OPRA/EQUS symbol parsing and row mapping."""
import csv
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Optional

import pandas as pd

from .. import config, paths, runner
from . import broker_script, price, session


# CME month character to month number mapping (for weekly expiries)
CME_MONTHS = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# OCC option symbol regex for parsing. Only the suffix is anchored (matches
# v4-golang's opraOCCTail): real Databento OPRA symbols space-pad the root
# to 6 chars (e.g. "NVDA  270115P00090000"), so anchoring the root at "^"
# with no whitespace allowance silently fails to match every row.
OCC_REGEX = re.compile(r"(\d{6})([CP])(\d{8})\s*$")

# GLBX weekly-option suffix, e.g. "ESZ7 P8250" / "ESM8 C9700" -> (P, 8250).
# Matches v4-golang's glbxCPStrike. Futures symbols (e.g. "ESZ7") never
# match this, so its absence is what identifies a plain future.
GLBX_OPTION_REGEX = re.compile(r"\s+([CP])(\d+(?:\.\d+)?)\s*$")

# TODO(xcme, still open): expiration. Measured on a real --all-symbols definition
# pull (2026-08-12, 961,438 unique instruments): 349,140 rows (36.3%) normalize to
# expiration == 0. Carrying instrument_class fixed classification but NOT this --
# expiration still comes from the regex below, and the count is unchanged.
#
# Root cause: GLBX_MONTH_YEAR_REGEX requires a SINGLE-digit year, but a large
# slice of GLBX uses two digits -- "BCXF27", "BCXQ26". "F27" fails to match
# (the regex wants one digit after the month code), so glbx_expiration_ns
# returns its no-match 0 and the contract lands with no expiration at all.
# This was invisible before because the basket path only ever fed it ~28
# hand-picked front-month roots, which all happen to be single-digit.
#
# Two ways out, in increasing order of correctness:
#
#  1. Widen to ([FGHJKMNQUVXZ])(\d{1,2})$ and extend the decade-rollover logic
#     in glbx_expiration_ns to treat a 2-digit group as an absolute year rather
#     than a decade offset. Cheap, but still *deriving* what we were handed.
#
#  2. Stop deriving. The --all-symbols definition path already carries the
#     authoritative values -- every one of those 349,140 rows has a real
#     expiration sitting in the raw CSV's end_ts (from record.pretty_expiration),
#     e.g. BCXF27 -> 2027-01-14 18:01Z. Widen MAPPING_COLUMNS to carry
#     instrument_class / strike_price / expiration through from InstrumentDefMsg
#     and have the mappers prefer them.
#
# Option 2 cannot be done by blindly reading end_ts here: the column is
# overloaded. On the definition path it is the contract's expiration; on the
# symbology.resolve path (which EQUS still uses, and which every basket run
# uses) it is d1, the mapping VALIDITY window end -- an unrelated date. Any fix
# has to distinguish the two sources before trusting the column, otherwise it
# corrupts the resolve venues to fix the definition one.
#
# Same caveat applies to OPRA: parse_occ_symbol is a regex over the OCC tail,
# and the definition path carries strike_price/expiration explicitly.
#
# The SECOND defect -- classifying everything as future-or-option -- is now
# fixed: map_xcme_row prefers instrument_class (see GLBX_CLASS_TYPES) and only
# falls back to the regex when the column is empty, which is every
# symbology.resolve row. Measured on the same pull, that moved 154,970 rows
# (16.1%) out of FUTURE and into FUTURE_SPREAD / OPTION_SPREAD / MIXED_SPREAD.
#
# Observed class histogram for GLBX.MDP3, 2026-08-11, all 961,438 instruments:
#   C CALL 380,056   P PUT 380,056   S FUTURE_SPREAD 91,981
#   T OPTION_SPREAD 46,968   F FUTURE 46,356   M MIXED_SPREAD 16,021
#
# Note what is NOT there on this session: no X (FX_SPOT), no Y, no B/K/I.
#
# That absence is NOT a reason to drop the X entry from GLBX_CLASS_TYPES. Databento
# split FX spots out of the futures class into instrument_class=X in the CME
# normalization change that reached production 2026-08-08, and this feed is already
# serving that normalization -- confirmed on 2026-08-11 data, where spread records
# carry the per-leg fields introduced by the same rollout (ESH7-ESM7 has
# leg_count=2, leg_raw_symbol='ESH7'). So X is the correct and current place to
# look; CME simply listed no FX spot instruments in this session's definitions, or
# they sit outside this key's entitlement. Keep the mapping: when a spot does
# appear it must not silently fall through to FUTURE, which is the exact bug the
# 2026-08-08 change was made to prevent.
#
# The same rollout also added leg_count / leg_raw_symbol / leg_instrument_class /
# leg_side / leg_ratio_*, which describe a spread's legs directly. Nothing here
# reads them yet -- a spread's underlying is still the symbol text -- but they are
# the authoritative source if spread legs ever need modelling.
#
# The scriptInstrumentType/scriptInstrumentType2 strings chosen for the spread
# classes are a guess at the house convention and want confirming: Nexus
# consumes them.

# US venues (XCME/XCBO) mirror Databento's own wire format: prices are
# fixed-point with a 1e-9 scale (e.g. a $1 price is the int64 1_000_000_000).
# Must be passed explicitly to price.scale_price -- its default scale is
# India's INDIA_PRICE_SCALE (1e5) for the Fyers/rupee path, not this one.
US_PRICE_SCALE = 10**9

# Raw rows read and mapped before the normalizer appends a batch to the output.
NORMALIZE_CHUNK_ROWS = 50_000

# Venue token prefix, concatenated onto the Databento instrument_id to make
# scriptToken globally unique across venues: XNAS 38 -> 11138, XCBO 637543226
# -> 222637543226. Databento only guarantees instrument_id is unique within a
# dataset, so the same id can name different contracts on two venues.
#
# The prefix is part of the key, not decoration: nothing downstream should strip
# it to recover the raw id. Digits only, so a prefixed token still passes any
# "is numeric" test.
#
# NOTE: prefixing pushes tokens past int32. Max observed today is XCBO
# 1509950237 -> 2221509950237 (13 digits, 2.2e12) and XCME 43049829 ->
# 33343049829; int32 tops out at 2147483647. Postgres scriptToken is already
# BIGINT and SQLite is TEXT, so both are fine -- but any consumer holding this
# in a 32-bit field will overflow.
VENUE_TOKEN_PREFIX = {
    "XNAS": "111",
    "XCBO": "222",
    "XCME": "333",
}


def prefixed_token(venue: str, instrument_id: Any) -> str:
    """Concatenate the venue prefix onto a Databento instrument_id.

    Returns the raw value unchanged for an unknown venue or a non-numeric id,
    rather than emitting a prefix glued to garbage.
    """
    raw = str(instrument_id).strip()
    # pandas widens an int column to float when any value is missing, which
    # renders ids as "637543226.0".
    if raw.endswith(".0"):
        raw = raw[:-2]
    prefix = VENUE_TOKEN_PREFIX.get(venue, "")
    if not prefix or not raw.isdigit():
        return raw
    return f"{prefix}{raw}"


@lru_cache(maxsize=1)
def _dotted_root_map() -> Dict[str, str]:
    """Map dot-stripped OCC roots back to their real tickers, e.g. BRKB -> BRK.B.

    OPRA symbology has no room for a dot: the class-share tickers BRK.B and BF.B
    are subscribed as BRKB.OPT / BFB.OPT and come back in OCC symbols as "BRKB",
    "BFB". Only the baskets know the real ticker, so they are the source here.

    A stripped form that could come from more than one basket entry is dropped
    rather than guessed -- an ambiguous mapping is worse than none.
    """
    out: Dict[str, str] = {}
    clashes: set[str] = set()
    for venue in ("XCBO", "XNAS"):
        path = paths.baskets_dir() / f"{venue}.csv"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            ticker = line.strip().upper()
            if not ticker or "." not in ticker:
                continue
            stripped = ticker.replace(".", "")
            if out.get(stripped, ticker) != ticker:
                clashes.add(stripped)
            out[stripped] = ticker
    for c in clashes:
        out.pop(c, None)
    return out


def dotted_underlying(root: str) -> str:
    """Restore the dot in a class-share root; pass anything else through."""
    if not root:
        return root
    return _dotted_root_map().get(root.upper(), root)


def parse_occ_symbol(symbol: str) -> Dict[str, Any]:
    """Parse OCC option symbol format: AAAA YYMMDD C/P 8-digit-strike."""
    s = symbol.strip()
    match = OCC_REGEX.search(s)
    if not match:
        return {}

    expiry_str, option_type, strike_str = match.groups()
    underlying = s[: match.start()].strip().replace(" ", "").upper()

    try:
        year = int(expiry_str[:2])
        month = int(expiry_str[2:4])
        day = int(expiry_str[4:6])

        # Two-digit year: 00-99, assume 00-30 = 2000-2030, 31-99 = 1931-1999
        full_year = 2000 + year if year <= 30 else 1900 + year

        strike = int(strike_str) / 1000.0  # Strike is encoded as integer (divide by 1000)

        return {
            "underlying": underlying,
            "expiration": f"{full_year:04d}{month:02d}{day:02d}",
            "option_type": "CALL" if option_type == "C" else "PUT",
            "strike": strike,
        }
    except (ValueError, IndexError):
        return {}


# Trailing CME month-code + single-digit year on a GLBX root/option base, e.g.
# "EWN6" -> ('N', '6') -> July, year digit 6. Distinct from OCC's 2-digit year.
GLBX_MONTH_YEAR_REGEX = re.compile(r"([FGHJKMNQUVXZ])(\d)$")


def glbx_expiration_ns(symbol_base: str, ref_date=None) -> int:
    """Extract expiration as nanosecond epoch UTC (matches fields.py's
    _expiration_ns convention) from a GLBX root's trailing month-code +
    single-digit year (e.g. "EWN6" -> July, year digit 6). Day is not encoded
    in the symbol, so it's simplified to the 1st of the month. Returns 0 when
    the symbol doesn't end in a recognized month/year code (e.g. the "parent"
    symbology entries GLBX also emits, which carry no contract month at all)."""
    match = GLBX_MONTH_YEAR_REGEX.search(symbol_base)
    if not match:
        return 0
    month = CME_MONTHS[match.group(1)]
    year_digit = int(match.group(2))

    anchor_year = ref_date.year if ref_date else datetime.now().year
    decade_base = anchor_year - (anchor_year % 10)
    year_full = decade_base + year_digit
    if year_full < anchor_year - 5:  # single digit wrapped past the decade boundary
        year_full += 10

    dt = datetime(year_full, month, 1, tzinfo=timezone.utc)
    return int(dt.timestamp()) * 10**9


def underlying_root_from_stype_in(symbol: str) -> str:
    """Extract underlying root from raw symbol."""
    # For ES futures, root is "ES"
    # For SPX options, root is "SPX"
    if symbol.startswith("."):
        return symbol[1:]  # Remove leading dot for index symbols
    # Interior dots are part of the ticker, not a separator: BRK.B and BF.B are
    # class-share tickers whose root is the whole thing. A bare ^[A-Z]+ stopped
    # at the dot and emitted "BRK" as the root of BRK.B.
    m = re.match(r"^[A-Z]+(?:\.[A-Z]+)*", symbol)
    return m.group(0) if m else symbol


def _fill_missing(result: Dict[str, Any]) -> Dict[str, Any]:
    for col in paths.NORMALIZED_COLUMNS:
        if col not in result:
            result[col] = ""
    return result


def _resolve_symbol_id_fallback(row: Dict[str, Any]) -> tuple[str, str, str]:
    """Extract (stype_in, stype_out, symbol) for a GLBX/OPRA/EQUS symbology row.

    Every venue's resolve() call here requests stype_out="instrument_id",
    which makes stype_out_symbol ("s") a bare numeric id rather than ticker
    text -- symbol must fall back to stype_in instead, or "script" ends up
    numeric. Confirmed on live data for all three venues (XCME, XCBO, XNAS);
    an earlier assumption that OPRA was exempt from this was wrong -- it
    just hadn't been checked against a real XCBO row yet, causing the same
    guard to be copy-pasted-and-dropped per-mapper before it was unified here.
    """
    stype_in = row.get("stype_in_symbol", "") or row.get("stype_in", "")
    stype_out = row.get("stype_out_symbol", "") or row.get("stype_out", "")
    if row.get("stype_out") == "instrument_id":
        stype_out = ""
    stype_in = stype_in if isinstance(stype_in, str) else str(stype_in)
    stype_out = stype_out if isinstance(stype_out, str) else str(stype_out)
    return stype_in, stype_out, stype_out or stype_in


GLBX_CLASS_TYPES = {
    # instrument_class -> (scriptInstrumentType, scriptInstrumentType2)
    "F": ("FUTIDX", "FUTURE"),
    "C": ("OPTIDX", "OPTION"),
    "P": ("OPTIDX", "OPTION"),
    "S": ("FUTIDX", "FUTURE_SPREAD"),
    "T": ("OPTIDX", "OPTION_SPREAD"),
    "M": ("FUTIDX", "MIXED_SPREAD"),
    "X": ("FXSPOT", "SPOT"),
    "Y": ("COMSPOT", "SPOT"),
    "B": ("BOND", "BOND"),
    "K": ("EQUITY", "EQUITY"),
    "I": ("INDEX", "INDEX"),
}


def map_xcme_row(row: Dict[str, Any], ref_date=None) -> Dict[str, Any]:
    """Map a GLBX/XCME symbology record to canonical schema.

    stype_out_symbol is only a real symbol string when stype_out is a symbol
    space (e.g. "raw_symbol"); when stype_out is "instrument_id" that column
    holds the numeric instrument id instead, so stype_in_symbol is the only
    reliable symbol text in that case.

    Classification prefers instrument_class, which only the definition path can
    supply. GLBX is not the two-valued future/option universe the symbol regex
    assumes -- it also carries calendar spreads, user-defined spreads, FX and
    commodity spots. Those have no C/P suffix, so regex-only classification
    calls every one of them a plain future. When the column is absent (any
    symbology.resolve row) the old regex branch still decides, so basket runs
    are unaffected.
    """
    stype_in, stype_out, symbol = _resolve_symbol_id_fallback(row)
    result = {
        "script": symbol,
        "scriptToken": prefixed_token("XCME", row.get("instrument_id", 0)),
        "scriptDetails": symbol,
        "currency": "USD",
        "exchange": "XCME",
        "multiplier": US_PRICE_SCALE,  # matches Databento's 1e-9 fixed-point wire price scale
        "tickSize": "",  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        "lotSize": 1,
        "tradingSessionUTC": session.trading_session_for_xcme(ref_date),
    }

    instrument_class = str(row.get("instrument_class", "") or "").strip().upper()

    # Strike and option type still come from the symbol: the C/P suffix carries
    # both, and it is present on exactly the rows instrument_class calls an
    # option. Only the future-or-not decision moves to instrument_class.
    opt_match = GLBX_OPTION_REGEX.search(symbol)
    if opt_match:
        base = symbol[: opt_match.start()].strip()
        result["optionType"] = "CALL" if opt_match.group(1) == "C" else "PUT"
        result["strike"] = price.scale_price(float(opt_match.group(2)), US_PRICE_SCALE)
    else:
        base = symbol
        result["optionType"] = ""
        result["strike"] = 0

    if instrument_class in GLBX_CLASS_TYPES:
        result["scriptInstrumentType"], result["scriptInstrumentType2"] = GLBX_CLASS_TYPES[instrument_class]
        # instrument_class is authoritative about being an option even when the
        # symbol carries no C/P suffix, so take the side from it in that case.
        if instrument_class in ("C", "P") and not result["optionType"]:
            result["optionType"] = "CALL" if instrument_class == "C" else "PUT"
    elif opt_match:
        result["scriptInstrumentType"] = "OPTIDX"
        result["scriptInstrumentType2"] = "OPTION"
    else:
        result["scriptInstrumentType"] = "FUTIDX"
        result["scriptInstrumentType2"] = "FUTURE"

    result["expiration"] = glbx_expiration_ns(base, ref_date)

    result["underlying_root"] = base
    result["underlying"] = base

    # Derived from the resolved expiration above, so the year in brokerScript1
    # always agrees with the expiration column.
    result["brokerScript1"] = broker_script.from_glbx(symbol, result["expiration"])
    broker_script.fill_unspecified(result)

    return _fill_missing(result)


def _session_close_ns(expiry_date, is_index: bool) -> int:
    """Epoch ns UTC for an option's actual expiration moment: the expiry
    date's own session close -- options expire intraday at market close, not
    at UTC midnight. expiry_date is the OCC/Databento expiry date, which is
    an ET calendar date, not UTC -- see session.xcbo_session_close_utc for
    why this can't be done by re-anchoring a formatted "HHMM" string to
    expiry_date in UTC.
    """
    dt = session.xcbo_session_close_utc(expiry_date, is_index)
    return int(dt.timestamp()) * 10**9


def map_xcbo_row(row: Dict[str, Any], ref_date=None) -> Dict[str, Any]:
    """Map an OPRA/XCBO symbology record to canonical schema."""
    stype_in, stype_out, symbol = _resolve_symbol_id_fallback(row)
    result = {
        "script": symbol,
        "scriptToken": prefixed_token("XCBO", row.get("instrument_id", 0)),
        "scriptDetails": symbol,
        "currency": "USD",
        "exchange": "XCBO",
        "multiplier": US_PRICE_SCALE,  # matches Databento's 1e-9 fixed-point wire price scale
        "tickSize": "",  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        "lotSize": 100,
        "scriptInstrumentType2": "OPTION",
    }

    parsed = parse_occ_symbol(symbol)
    underlying = parsed.get("underlying", "") if parsed else ""
    is_index = underlying.upper() in ("SPX", "SPXW", "VIX", "RUT")

    if parsed:
        # OCC symbols cannot carry a dot, so BRK.B trades as root "BRKB". Map it
        # back for the underlying columns only -- brokerScript1 is built from
        # `parsed` further down and must stay dotless (BRKB/280121/410C), which
        # is why `parsed` is deliberately not mutated here.
        result["underlying"] = dotted_underlying(underlying)
        result["underlying_root"] = dotted_underlying(underlying).upper()
        result["strike"] = price.scale_price(parsed.get("strike", 0), US_PRICE_SCALE)
        result["optionType"] = parsed.get("option_type", "")
        expiry_date = datetime.strptime(parsed.get("expiration", "20240101"), "%Y%m%d").date()
        result["expiration"] = _session_close_ns(expiry_date, is_index)
    else:
        result["underlying"] = ""
        result["underlying_root"] = ""
        result["strike"] = 0
        result["optionType"] = ""
        result["expiration"] = 0

    # Uses the OCC-embedded date, not result["expiration"] -- the latter is the
    # session close in UTC and can fall on the next calendar day.
    result["brokerScript1"] = broker_script.from_occ(symbol, parsed)
    broker_script.fill_unspecified(result)

    result["scriptInstrumentType"] = "OPTIDX" if is_index else "OPTSTK"
    result["tradingSessionUTC"] = (
        session.trading_session_for_xcbo_index(ref_date) if is_index else session.trading_session_for_xcbo_equity(ref_date)
    )

    return _fill_missing(result)


def map_xnas_row(row: Dict[str, Any], ref_date=None) -> Dict[str, Any]:
    """Map an EQUS/XNAS symbology record to canonical schema.

    Equities have no strike/expiration/option fields at all -- kept separate
    from the option venues rather than branching those fields to empty.

    Like XCME (see map_xcme_row), EQUS resolves raw_symbol -> instrument_id,
    so stype_out_symbol ("s") is a bare numeric id here, not a ticker --
    must fall back to stype_in or "script" ends up numeric instead of the
    symbol string.
    """
    stype_in, stype_out, symbol = _resolve_symbol_id_fallback(row)
    result = {
        "script": symbol,
        "scriptToken": prefixed_token("XNAS", row.get("instrument_id", 0)),
        "scriptDetails": symbol,
        "currency": "USD",
        "exchange": "XNAS",
        "scriptInstrumentType": "EQUITY",
        "scriptInstrumentType2": "EQUITY",
        "underlying_root": underlying_root_from_stype_in(stype_in),
        "underlying": symbol,
        "strike": 0,
        "optionType": "",
        "multiplier": US_PRICE_SCALE,  # matches Databento's 1e-9 fixed-point wire price scale
        "tickSize": "",  # NULL: tickSize is exclusive to the interactive layer, Nexus doesn't depend on it
        "lotSize": 1,
        "expiration": 0,  # No expiration for equities
        "tradingSessionUTC": session.trading_session_for_xnas(ref_date),
        "brokerScript1": broker_script.from_equity(symbol),
    }
    broker_script.fill_unspecified(result)

    return _fill_missing(result)


VENUE_MAPPERS = {
    "xcme": map_xcme_row,
    "xcbo": map_xcbo_row,
    "xnas": map_xnas_row,
}


def run(opts: runner.Opts) -> None:
    """Normalize Databento data: stream each raw CSV, map symbols, append output.

    Reads and writes in chunks rather than whole files. An --all-symbols GLBX
    pull is ~961k rows, which the old read_csv/iterrows/DataFrame path held
    three times over -- as a source frame, as a list of mapped dicts, and again
    as an output frame -- before writing anything. Mirrors the streaming the
    download side already does, for the same reason.
    """
    if opts.dry_run:
        print("DRY RUN: Would normalize Databento data")
        return

    print("  Normalizing Databento data...")
    normalized_dir = paths.normalized_dir(opts.date_dir)
    ref_date = datetime.strptime(opts.date_dir, "%Y%m%d").date()

    normalized_dir.mkdir(parents=True, exist_ok=True)

    # Process each Databento venue independently -- stype_in/stype_out
    # semantics differ per venue, so each gets its own mapper.
    for venue, mapper in VENUE_MAPPERS.items():
        csv_path = paths.databento_raw_csv(opts.date_dir, venue)
        if not csv_path.exists():
            continue

        print(f"    Processing {venue}...")
        output_path = normalized_dir / f"{venue.upper()}-DATABENTO-normalized.csv"
        # PID-scoped staging, same rationale as the download side: two runs must
        # not share one temp path, and readers must never see a partial file.
        temp_path = output_path.with_suffix(f".tmp.{os.getpid()}.csv")

        total = 0
        try:
            with open(temp_path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=paths.NORMALIZED_COLUMNS, extrasaction="ignore", restval=""
                )
                writer.writeheader()
                # dtype=str keeps ids and symbols verbatim: pandas otherwise widens
                # an int column to float wherever a value is missing, which is what
                # turned instrument ids into "637543226.0".
                for frame in pd.read_csv(
                    csv_path, chunksize=NORMALIZE_CHUNK_ROWS, dtype=str, keep_default_na=False
                ):
                    batch = []
                    for row in frame.to_dict("records"):
                        norm_row = mapper(row, ref_date)
                        if norm_row.get("script"):
                            batch.append(norm_row)
                    if not batch:
                        continue
                    writer.writerows(batch)
                    fh.flush()
                    total += len(batch)
                    print(f"      {total} row(s)...", flush=True)
        except Exception as e:
            print(f"      Error processing {csv_path}: {e}")
            temp_path.unlink(missing_ok=True)
            continue

        if total:
            temp_path.replace(output_path)
            print(f"      Wrote {total} rows")
        else:
            # A header-only file would look like a valid empty venue downstream.
            temp_path.unlink(missing_ok=True)
            print(f"      No rows for {venue}")
