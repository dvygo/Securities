"""Databento normalization: GLBX/OPRA/EQUS symbol parsing and row mapping."""
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List

import databento as db
import pandas as pd

from .. import config, parquet_export, paths, runner
from ..sources import databento_src as ds
from . import broker_script, counter_token, price, session


# CME month character to month number mapping (for weekly expiries)
CME_MONTHS = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# OCC option symbol regex for parsing. Only the suffix is anchored: real
# Databento OPRA symbols space-pad the root
# to 6 chars (e.g. "NVDA  270115P00090000"), so anchoring the root at "^"
# with no whitespace allowance silently fails to match every row.
OCC_REGEX = re.compile(r"(\d{6})([CP])(\d{8})\s*$")

# GLBX weekly-option suffix, e.g. "ESZ7 P8250" / "ESM8 C9700" -> (P, 8250).
# Futures symbols (e.g. "ESZ7") never match this, so its absence is what identifies a plain future.
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

# OPRA returns the underlying spots alongside the options on an --all-symbols
# pull, classed STOCK. They are typed SPOT here rather than EQUITY: the equity
# listing is XNAS's, and this row is only OPRA's reference leg for it.
XCBO_SPOT_CLASS = "K"

# counterToken numbering lives in counter_token.py, shared with the Fyers path.

def prefixed_token(venue: str, instrument_id: Any) -> str:
    """Return the Databento instrument_id as scriptToken, unprefixed.

    This used to namespace the id with a per-venue prefix (XNAS 111, XCBO 222,
    XCME 333) because Databento only guarantees instrument_id is unique WITHIN a
    dataset -- the same id can name different contracts on two venues -- and the
    downstream pg symbol-master table keys on (token, trade_date) with no
    exchange column. That prefixing is removed by request; the venue argument is
    kept so call sites and the CSV schema are unchanged.

    Consequence, stated plainly: tokens are once again only unique per venue, so
    two venues pushed to the same table on the same trade_date can collide.
    """
    raw = str(instrument_id).strip()
    # pandas widens an int column to float when any value is missing, which
    # renders ids as "637543226.0". Unrelated to prefixing, so it stays.
    if raw.endswith(".0"):
        raw = raw[:-2]
    return raw


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


def _copy_definition_fields(row: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Carry the raw definition fields through to the normalized row, prefixed.

    Done here rather than inside each venue mapper because it is the same verbatim
    copy for all three, and the mappers are about deriving canonical columns.

    Only the ALL_SYMBOLS definition path writes these; a symbology.resolve CSV has
    no such columns and every one of them stays the "" that _fill_missing already
    put there. That is also why this cannot use `or ""` on a missing key and be
    done -- the canonical columns must not be touched, so it only ever writes the
    prefixed names.
    """
    for field_name, column in zip(paths.DEFINITION_FIELDS, paths.DEFINITION_PASSTHROUGH_COLUMNS):
        value = row.get(field_name)
        if value not in (None, ""):
            result[column] = value


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


def _manual_dbn_scripts(files: List[Path], expected_dataset: str) -> Iterator[str]:
    """Just the raw_symbols from a manual DBN drop, in the same order and with
    the same dedupe as _manual_dbn_row_batches.

    counterTokenV2's collect pass needs the day's symbol set and nothing else.
    Going through _manual_dbn_row_batches for that costs ~90s on OPRA, because
    it stringifies every DEFINITION_FIELDS column for all 2M records -- 60M
    getattr+str calls -- and then the collect pass throws all of it away. Reading
    one attribute instead makes the pass ~0.4s. Measured 2026-08-26.

    The dedupe and the schema/dataset guards have to match the full reader
    exactly, or the symbol set would disagree with the rows actually written.
    """
    seen: set = set()
    for path in files:
        store = db.DBNStore.from_file(path)
        if str(store.schema) != "definition" or str(store.dataset) != expected_dataset:
            continue
        for rec in store:
            if not isinstance(rec, db.InstrumentDefMsg):
                continue
            if rec.instrument_id in seen:
                continue
            seen.add(rec.instrument_id)
            yield rec.raw_symbol


def script_of(row: Dict[str, Any]) -> str:
    """The `script` a mapper would produce, without running the whole mapper.

    All three Databento mappers set script from _resolve_symbol_id_fallback's
    symbol, so counterTokenV2's collect pass can read the day's symbol set
    without paying for broker symbology, session and price derivation on every
    row a second time.
    """
    return _resolve_symbol_id_fallback(row)[2]


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
    """Map an OPRA/XCBO symbology record to canonical schema.

    OPRA is not options-only. An --all-symbols pull also returns the underlying
    spots, which arrive as instrument_class=K with a bare ticker and no OCC tail
    -- 6,323 of them against ~2.03M options on 2026-08-13 (QQQ, AMD, MU, ...).
    parse_occ_symbol cannot match those, which left underlying empty, is_index
    False, and every one of them typed OPTSTK: an option row with no strike, no
    expiry and no option type.

    They are typed SPOT/SPOT instead, deliberately not EQUITY -- equities are
    XNAS's job, and these exist here only as the reference leg for OPRA options.

    Only the definition path carries instrument_class; on a symbology.resolve row
    the column is empty and the OCC-regex path below decides exactly as before.
    """
    stype_in, stype_out, symbol = _resolve_symbol_id_fallback(row)
    instrument_class = str(row.get("instrument_class", "") or "").strip().upper()
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

    if instrument_class == XCBO_SPOT_CLASS:
        # The symbol IS the ticker here -- there is no OCC tail to strip, and no
        # dot to restore either, since a spot is listed under its real ticker.
        result["scriptInstrumentType"] = "SPOT"
        result["scriptInstrumentType2"] = "SPOT"
        result["underlying"] = symbol
        result["underlying_root"] = symbol.upper()
        result["strike"] = 0
        result["optionType"] = ""
        result["expiration"] = 0  # a spot never expires
        result["brokerScript1"] = broker_script.from_equity(symbol)
    else:
        # Uses the OCC-embedded date, not result["expiration"] -- the latter is the
        # session close in UTC and can fall on the next calendar day.
        result["brokerScript1"] = broker_script.from_occ(symbol, parsed)
        result["scriptInstrumentType"] = "OPTIDX" if is_index else "OPTSTK"

    broker_script.fill_unspecified(result)

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


def _manual_dbn_files(directory: Path) -> List[Path]:
    """*.dbn/*.dbn.zst files in a manual drop directory, sorted by name.

    condition.json/metadata.json/manifest.json (whatever else the operator
    extracted from a batch job's zip alongside the payload) are ignored by
    construction -- only the DBN suffix is matched.
    """
    return sorted(p for p in directory.iterdir() if p.name.endswith((".dbn", ".dbn.zst")))


def _manual_dbn_row_batches(
    files: List[Path], expected_dataset: str, chunk_rows: int
) -> Iterator[List[Dict[str, Any]]]:
    """Definition rows from one or more manually-dropped DBN files, batched.

    Yields the same row shape databento_src.py's own _iter_definition_batches
    produces off a live download, so the mapper/passthrough code below treats a
    manual drop identically to a streamed one. Deduped on instrument_id across
    all files together (first file wins, sorted by filename), matching that
    same download-side dedupe -- a definition schema restates the same
    instrument intraday, and stacking several sessions' files should still
    resolve each instrument once.

    A file whose schema isn't "definition", or whose dataset doesn't match the
    venue folder it was dropped in, is skipped with a warning rather than
    silently mixed in -- a misplaced file (wrong venue's export, or a trades/
    mbo dump instead of a definition one) would otherwise corrupt the venue's
    normalized output with no error at all.
    """
    seen: set = set()
    batch: List[Dict[str, Any]] = []
    for path in files:
        store = db.DBNStore.from_file(path)
        if str(store.schema) != "definition":
            print(f"      Skipping {path.name}: schema={store.schema}, expected definition")
            continue
        if str(store.dataset) != expected_dataset:
            print(f"      Skipping {path.name}: dataset={store.dataset}, expected "
                  f"{expected_dataset} -- wrong venue folder?")
            continue
        for rec in store:
            if not isinstance(rec, db.InstrumentDefMsg):
                continue
            if rec.instrument_id in seen:
                continue
            seen.add(rec.instrument_id)
            row = {
                "instrument_id": rec.instrument_id,
                "stype_in_symbol": rec.raw_symbol,
                "stype_out_symbol": rec.instrument_id,
                "stype_in": "raw_symbol",
                "stype_out": "instrument_id",
                "start_ts": rec.pretty_activation,
                "end_ts": rec.pretty_expiration,
                "instrument_class": str(rec.instrument_class),
            }
            row.update({f: ds._def_value(rec, f) for f in paths.DEFINITION_FIELDS})
            batch.append(row)
            if len(batch) >= chunk_rows:
                yield batch
                batch = []
    if batch:
        yield batch


def _csv_row_batches(csv_path: Path, chunk_rows: int) -> Iterator[List[Dict[str, Any]]]:
    """Rows from a streamed-download raw CSV, batched.

    dtype=str keeps ids and symbols verbatim: pandas otherwise widens an int
    column to float wherever a value is missing, which is what turned
    instrument ids into "637543226.0".
    """
    for frame in pd.read_csv(csv_path, chunksize=chunk_rows, dtype=str, keep_default_na=False):
        yield frame.to_dict("records")


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
    # Numbering config is pre-flighted once, before a single row is written.
    # A base that would bleed int32, collide with another venue's block, or is
    # simply unset is a CRITICAL config error -- the venue is skipped rather
    # than normalized into tokens a 32-bit consumer cannot hold.
    token_errors = counter_token.validate(config.load_exchanges())
    mine = {ds.VENUE_CONFIGS[v].venue_name for v in VENUE_MAPPERS if v in ds.VENUE_CONFIGS}
    for mic in sorted(set(token_errors) & mine):
        for msg in token_errors[mic]:
            print(f"  CRITICAL [{mic}] counterToken config: {msg}")
        print(f"  CRITICAL: skipping {mic} -- fix conf/config.ini [EXCHANGE:{mic}] "
              f"before normalizing")

    for venue, mapper in VENUE_MAPPERS.items():
        venue_cfg = ds.VENUE_CONFIGS[venue]
        if venue_cfg.venue_name in token_errors:
            continue
        manual_dir = paths.manual_venue_dir(opts.date_dir, venue_cfg.venue_name)
        manual_files = _manual_dbn_files(manual_dir) if manual_dir.is_dir() else []
        csv_path = paths.databento_raw_csv(opts.date_dir, venue)

        if manual_files:
            # A manual drop wins over the streamed CSV when both exist for the
            # same day -- it is a deliberate operator override, e.g. a batch
            # job's definition file dropped in place of the day's own
            # --all-symbols download.
            source = f"{len(manual_files)} manual file(s) in {manual_dir.name}/"

            def _row_batches(files=manual_files, dataset=venue_cfg.dataset):
                return _manual_dbn_row_batches(files, dataset, NORMALIZE_CHUNK_ROWS)

            def _source_scripts(files=manual_files, dataset=venue_cfg.dataset):
                return _manual_dbn_scripts(files, dataset)
        elif csv_path.exists():
            source = csv_path.name

            def _row_batches(path=csv_path):
                return _csv_row_batches(path, NORMALIZE_CHUNK_ROWS)

            def _source_scripts(path=csv_path):
                return (script_of(row) for batch in _csv_row_batches(path, NORMALIZE_CHUNK_ROWS)
                        for row in batch)
        else:
            continue
        row_batches = _row_batches()

        print(f"    Processing {venue} ({source})...")
        output_path = normalized_dir / f"{venue.upper()}-DATABENTO-normalized{parquet_export.SUFFIX}"

        # counterTokenV2 needs the whole day's symbol set before it can allocate:
        # which offsets are free depends on which of yesterday's scripts are
        # absent today, and that is not known until the last row. This path
        # streams in chunks and cannot buffer 2M mapped rows, so the symbol set
        # is collected in a cheap first pass over the same source -- script_of
        # instead of the full mapper -- and the write below stays streaming.
        mic = venue_cfg.venue_name
        exchange_cfg = counter_token.exchange_for(venue)
        tokens = None
        if exchange_cfg is not None and exchange_cfg.venue_id:
            try:
                previous, prev_day = counter_token.previous_tokens(
                    opts.date_dir, mic, exchange_cfg.venue_id)
            except ValueError as exc:
                print(f"      CRITICAL: skipping {venue} -- {exc}")
                continue
            scripts = [script for script in _source_scripts() if script]
            tokens = counter_token.carry_forward(
                previous, scripts, exchange_cfg.venue_id, exchange_cfg.counter_prefix_v2)
            counter_token.check_capacity(mic, exchange_cfg.counter_prefix_v2, tokens.high_water)
            new_count = len(tokens.assigned) - (
                0 if previous is None
                else len(set(tokens.assigned) & set(previous.assigned)))
            print(f"      counterTokenV2: {len(tokens.assigned):,} symbol(s), "
                  f"{new_count:,} new, high-water {tokens.high_water:,}"
                  + (f", carried from {prev_day}" if previous else ", first day"))
            row_batches = _row_batches()

        prefix = counter_token.prefix_for(venue)
        # PID-scoped staging and promote-on-close live in RowWriter, for the same
        # reason the download side stages: two runs must not share one temp path,
        # and readers must never see a partial file. A staging name that still
        # looked like a finished venue file is what once got a killed run's prefix
        # mapped into plugin/ and pushed, dying on the (token, trade_date) key.
        writer = parquet_export.RowWriter(output_path, paths.NORMALIZED_COLUMNS)
        total = 0
        try:
            for rows in row_batches:
                batch = []
                for row in rows:
                    norm_row = mapper(row, ref_date)
                    if norm_row.get("script"):
                        _copy_definition_fields(row, norm_row)
                        batch.append(norm_row)
                if not batch:
                    continue
                # Numbered after the script filter so the sequence has no gaps.
                # `total` carries the counter across chunks, keeping it gapless
                # and unique for the whole venue rather than per batch.
                if prefix is not None:
                    for n, r in enumerate(batch, total + 1):
                        r["counterToken"] = counter_token.assign(prefix, n)
                if tokens is not None:
                    for r in batch:
                        r["counterTokenV2"] = tokens.token(r.get("script", ""))
                writer.write(batch)
                total += len(batch)
                print(f"      {total} row(s)...", flush=True)
        except Exception as e:
            print(f"      Error processing {source}: {e}")
            writer.abort()
            continue

        # close() writes nothing at all for an empty venue -- a row-group-less
        # file would look like a valid empty venue downstream.
        if writer.close():
            # Only after the file is promoted: a manifest naming tokens that no
            # output actually carries would be read as tomorrow's truth.
            if tokens is not None:
                counter_token.merge_into_manifest(opts.date_dir, mic, tokens)
            print(f"      Wrote {total} rows to {output_path.name}")
        else:
            print(f"      No rows for {venue}")
