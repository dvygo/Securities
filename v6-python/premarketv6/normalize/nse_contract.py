"""NSE official contract masters -> the canonical schema, for XNSE.

Replaces the three Fyers CSVs (XNSE/XNFO/XNCD) with the exchange's own files,
which the broker drops in as a folder each day:

    data/YYYYMMDD/XNSE/NEW FILE FORMAT/
        NSE_CM_security.csv     cash market      -> equities, ETFs, debt, G-secs
        NSE_FO_contract.csv     futures/options  -> FUTIDX FUTSTK OPTIDX OPTSTK
        NSE_CD_contract.csv     currency derivs  -> FUTCUR OPTCUR FUTIRC FUTIRT

Everything else in that folder is ignored on purpose. contract.txt and
security.txt are the legacy pipe-delimited NEAT renderings of the same records
(identical counts), the two spdcontract files are spreads, and fo_participant.txt
is a broker registry rather than instruments. They stay on disk untouched.

Three things about this format cost real effort to establish and are asserted
here rather than rediscovered:

DATES USE A 1980 EPOCH, NOT UNIX. XpryDt 1475159400 is 2026-09-29, not
2016-09-29. Checked against the whole file: under the 1980 epoch all 76,484
dated contracts expire on or after the file's own date; under Unix only 18 do.
Getting this wrong shifts every date by exactly ten years and still produces a
plausible-looking date, which is the dangerous kind of wrong.

PRICES ARE PRE-SCALED, AND THE SCALE DIFFERS BY SEGMENT. Cash and F&O arrive
x100; currency derivatives arrive x10,000,000. Confirmed two independent ways --
the strike encoded in the contract name (EURINR26O09116.25CE carries StrkPric
1162500000) and the standard 0.0025 currency tick. DcmlstnPric says "4" for the
currency file and does NOT give the scale; do not trust it.

EXPIRY TIME IS NOT IN THE FILE. XpryDt lands at 20:00 IST. The pipeline's
convention, inherited from the Fyers feed this replaces, is the session close:
10:10 UTC for F&O and 07:00 UTC for currency. Converting the date and applying
the segment's close reproduces the previous feed's timestamps exactly.
"""
import csv
import datetime as dt
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .. import paths, parquet_export, runner, config
from . import broker_script, counter_token, fields as fyers_fields

# The drop folder, named by the broker and kept verbatim so an operator sees the
# same string in the pipeline as on disk.
DROP_DIR = "NEW FILE FORMAT"

# Seconds between 1970-01-01 and 1980-01-01 (3652 days: ten years plus the 1972
# and 1976 leap days). NSE counts from the latter.
NSE_EPOCH_OFFSET = 315_532_800

CM_FILE = "NSE_CM_security.csv"
FO_FILE = "NSE_FO_contract.csv"
CD_FILE = "NSE_CD_contract.csv"

# Per segment: price scale, the session string, and the UTC time of day an
# expiry lands on. The sessions are the values the Fyers feed carried for these
# instruments, kept identical so the switch moves no column but the source.
CASH, DERIV, CURRENCY = "CASH", "DERIV", "CURRENCY"
SEGMENTS = {
    CASH:     {"scale": 100,        "session": "0330-0338|0345-1000|1245-1345", "close": None},
    DERIV:    {"scale": 100,        "session": "0345-1010|1245-1345",           "close": (10, 10)},
    CURRENCY: {"scale": 10_000_000, "session": "0330-1130|1245-1345",           "close": (7, 0)},
}

# Cash-market series -> the instrument type the pipeline uses. Derived by
# joining the previous feed's output against this file on script, so these are
# observed rather than assumed.
SERIES_EQUITY = {"EQ", "SM", "BE", "ST", "BZ", "RR", "E1", "IT", "SZ"}
SERIES_TYPES = {
    "SG": "SGB", "GS": "G-SECS", "MF": "MF", "TB": "T-BILLS",
    "W1": "WARRANTS", "SF": "MISC", "D1": "MISC",
}
# Everything not named above is a debt series. NSE runs well over a hundred of
# them (N0..N9, NA..NZ, Y*, Z*, GB and more) and every one that the previous
# feed also carried came through as DEBENTURES, so this is the observed default
# rather than a guess -- but unknown series are counted and printed, so a new
# one shows up in the log instead of hiding inside the default.
SERIES_DEFAULT = "DEBENTURES"

# SctyTpFlg splits a series that holds two kinds of thing. Nothing else in the
# file does -- every other type column is blank for both. The flag is
# CONTEXTUAL, not a type code: "4" means an ETF inside series EQ and an InvIT
# inside series IV, so it is read per series rather than globally.
ETF_FLAG = "4"
SERIES_BY_FLAG = {
    "EQ": {"0": "EQ", ETF_FLAG: "ETF"},
    "IV": {"0": "EQ", ETF_FLAG: "MISC"},
}

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def drop_dir(as_of: str) -> Path:
    """The broker's daily folder inside the XNSE venue directory."""
    return paths.venue_dir(as_of, "XNSE") / DROP_DIR


def present(as_of: str) -> bool:
    """True when the day has the three files this reads."""
    directory = drop_dir(as_of)
    return directory.is_dir() and all(
        (directory / name).exists() for name in (CM_FILE, FO_FILE, CD_FILE))


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def to_unix_seconds(nse_seconds) -> int:
    """NSE's 1980-epoch seconds -> Unix seconds. 0 for absent or sentinel."""
    raw = _int(nse_seconds, 0)
    if raw <= 0:
        return 0
    return raw + NSE_EPOCH_OFFSET


def expiration_ns(nse_seconds, close: Optional[Tuple[int, int]]) -> int:
    """Expiry as epoch nanoseconds at the segment's session close.

    The file's own time of day (20:00 IST) is discarded deliberately: the column
    means "last eligible trade time" everywhere else in this pipeline, and the
    previous feed populated it with the session close. Keeping the file's value
    would silently move every India expiry by four hours and twenty minutes.
    """
    unix = to_unix_seconds(nse_seconds)
    if unix <= 0 or close is None:
        return 0
    day = dt.datetime.fromtimestamp(unix, tz=dt.timezone.utc).date()
    at = dt.datetime(day.year, day.month, day.day, close[0], close[1],
                     tzinfo=dt.timezone.utc)
    return int(at.timestamp()) * 1_000_000_000


def _human_date(unix_seconds: int) -> str:
    """"04 Sep 26", the form the previous feed used in scriptDetails."""
    if unix_seconds <= 0:
        return ""
    d = dt.datetime.fromtimestamp(unix_seconds, tz=dt.timezone.utc)
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year % 100:02d}"


def _strike_text(raw: str, scale: int) -> str:
    """Strike in human units for the description: 7260000 @x100 -> "72600"."""
    value = _int(raw, 0)
    if value <= 0:
        return ""
    text = f"{value / scale:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def cash_instrument_type(row: Dict[str, str],
                         by_isin: Optional[Dict[str, str]] = None) -> str:
    """Instrument type for a cash-market row, in three tiers.

    1. The series, where we have evidence for it.
    2. Failing that, the same ISIN in a series we DO have evidence for. NSE runs
       the same security in several trading windows -- BL is the block-deal
       book, RL the retail lot, IQ, SL, SQ, T0 others -- and every one of those
       carries the ISIN of an ordinary equity. This tier alone resolves 13,462
       of the 18,768 rows the series table cannot name, all of them to EQ or ETF.
    3. Failing that, DEBENTURES. What is left is N0/U0/N1/U1/L1/R1/V1 and
       friends, which are debt series: "ABCL 0% 2031 SR C2" and the like.

    An earlier attempt read the security type out of ISIN positions 9-11. That
    is wrong -- they are a serial, and known equities carry 01, 02, 03, 04 and
    05 there exactly as unclassified rows do.
    """
    series = (row.get("SctySrs") or "").strip().upper()
    if series in SERIES_BY_FLAG:
        flag = (row.get("SctyTpFlg") or "").strip()
        return SERIES_BY_FLAG[series].get(flag, SERIES_BY_FLAG[series]["0"])
    if series in SERIES_TYPES:
        return SERIES_TYPES[series]
    if series in SERIES_EQUITY:
        return "EQ"
    if by_isin:
        found = by_isin.get((row.get("ISIN") or "").strip())
        if found:
            return found
    return SERIES_DEFAULT


def _types_by_isin(rows) -> Dict[str, str]:
    """ISIN -> instrument type, from the rows whose series we can name."""
    index: Dict[str, str] = {}
    for row in rows:
        series = (row.get("SctySrs") or "").strip().upper()
        isin = (row.get("ISIN") or "").strip()
        if not isin or isin in index:
            continue
        if series in SERIES_BY_FLAG:
            flag = (row.get("SctyTpFlg") or "").strip()
            index[isin] = SERIES_BY_FLAG[series].get(flag, SERIES_BY_FLAG[series]["0"])
        elif series in SERIES_TYPES:
            index[isin] = SERIES_TYPES[series]
        elif series in SERIES_EQUITY:
            index[isin] = "EQ"
    return index


def map_cash_row(row: Dict[str, str],
                 by_isin: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """NSE_CM_security.csv row -> canonical schema."""
    ticker = (row.get("TckrSymb") or "").strip()
    series = (row.get("SctySrs") or "").strip().upper()
    if not ticker or not series:
        return None

    script = f"NSE:{ticker}-{series}"
    inst = cash_instrument_type(row, by_isin)
    out = {
        "script": script,
        "scriptToken": (row.get("FinInstrmId") or "").strip(),
        "scriptDetails": (row.get("FinInstrmNm") or "").strip() or ticker,
        "scriptInstrumentType": inst,
        "scriptInstrumentType2": fyers_fields.instrument_type2(inst),
        "ISIN": (row.get("ISIN") or "").strip(),
        "multiplier": SEGMENTS[CASH]["scale"],
        "lotSize": _int(row.get("NewBrdLotQty"), 1) or 1,
        "tickSize": _int(row.get("BidIntrvl"), 0),
        "tradingSessionUTC": SEGMENTS[CASH]["session"],
        "expiration": 0,
        "underlying": ticker,
        "underlying_root": ticker,
        "strike": 0,
        "optionType": "",
        "currency": "INR",
    }
    out["brokerScript1"] = broker_script.from_equity(script)
    broker_script.fill_unspecified(out)
    return out


def map_derivative_row(row: Dict[str, str], segment: str) -> Optional[Dict[str, str]]:
    """NSE_FO_contract.csv / NSE_CD_contract.csv row -> canonical schema.

    The contract name in StockNm IS the previous feed's script without its
    "NSE:" prefix -- verified against a full day, where 99.4% of the previous
    feed's symbols reappear unchanged. That is what lets counterTokenV2 carry
    forward across the source switch instead of renumbering the venue.
    """
    name = (row.get("StockNm") or "").strip()
    ticker = (row.get("TckrSymb") or "").strip()
    if not name or not ticker:
        return None

    conf = SEGMENTS[segment]
    scale = conf["scale"]
    script = f"NSE:{name}"
    inst = (row.get("FinInstrmNm") or "").strip().upper()
    option = (row.get("OptnTp") or "").strip().upper()
    unix = to_unix_seconds(row.get("XpryDt"))
    strike_raw = _int(row.get("StrkPric"), 0)

    parts = [ticker, _human_date(unix)]
    if option in ("CE", "PE"):
        parts.append(_strike_text(row.get("StrkPric"), scale))
        parts.append(option)
    else:
        parts.append("FUT")

    out = {
        "script": script,
        "scriptToken": (row.get("FinInstrmId") or "").strip(),
        "scriptDetails": " ".join(p for p in parts if p),
        "scriptInstrumentType": inst,
        "scriptInstrumentType2": fyers_fields.instrument_type2(inst),
        "ISIN": (row.get("ISIN") or "").strip(),
        "multiplier": scale,
        "lotSize": _int(row.get("NewBrdLotQty"), 1) or 1,
        "tickSize": _int(row.get("BidIntrvl"), 0),
        "tradingSessionUTC": conf["session"],
        "expiration": expiration_ns(row.get("XpryDt"), conf["close"]),
        "underlying": ticker,
        "underlying_root": ticker,
        "strike": strike_raw if strike_raw > 0 else 0,
        "optionType": "CALL" if option == "CE" else ("PUT" if option == "PE" else ""),
        "currency": "INR",
    }
    out["brokerScript1"] = broker_script.from_equity(script)
    broker_script.fill_unspecified(out)
    return out


def _read(path: Path) -> Iterator[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
        yield from csv.DictReader(handle)


def rows_for(as_of: str) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Every instrument in the day's drop, and a per-file count."""
    directory = drop_dir(as_of)
    rows: List[Dict[str, str]] = []
    counts: Dict[str, int] = {}
    unknown_series: Dict[str, int] = {}

    # Read the cash file once to index ISIN -> type, so tier 2 of the
    # classification can see the whole file rather than only rows before it.
    cash = list(_read(directory / CM_FILE))
    by_isin = _types_by_isin(cash)

    for row in cash:
        mapped = map_cash_row(row, by_isin)
        if mapped is None:
            continue
        series = (row.get("SctySrs") or "").strip().upper()
        if (series not in SERIES_TYPES and series not in SERIES_EQUITY
                and series not in SERIES_BY_FLAG
                and not by_isin.get((row.get("ISIN") or "").strip())):
            unknown_series[series] = unknown_series.get(series, 0) + 1
        rows.append(mapped)
    counts[CM_FILE] = len(rows)

    for name, segment in ((FO_FILE, DERIV), (CD_FILE, CURRENCY)):
        before = len(rows)
        for row in _read(directory / name):
            mapped = map_derivative_row(row, segment)
            if mapped is not None:
                rows.append(mapped)
        counts[name] = len(rows) - before

    if unknown_series:
        top = sorted(unknown_series.items(), key=lambda kv: -kv[1])[:8]
        print(f"    {sum(unknown_series.values()):,} row(s) in {len(unknown_series)} "
              f"series named by neither the series table nor a matching ISIN, "
              f"defaulted to {SERIES_DEFAULT}: "
              + ", ".join(f"{s}={n:,}" for s, n in top))

    # counterTokenV2 maps script -> token, so two rows sharing a script would
    # share a token and the one-to-one invariant would no longer hold. NSE lists
    # a handful of interest-rate underlyings twice under one name with different
    # instrument ids; the first wins and the collision is reported rather than
    # silently carried into the numbering.
    seen: Dict[str, int] = {}
    unique: List[Dict[str, str]] = []
    collisions = 0
    for row in rows:
        script = row.get("script", "")
        if script in seen:
            collisions += 1
            continue
        seen[script] = 1
        unique.append(row)
    if collisions:
        print(f"    {collisions:,} row(s) dropped: a script already claimed by an "
              f"earlier row -- one token per script")
    return unique, counts


OUTPUT = "XNSE-NSE.parquet"


def run(opts: runner.Opts) -> None:
    """Normalize step: the XNSE venue, from the exchange's own contract masters.

    Numbering is the same contract every other venue keeps -- open the shared
    sequence, carry the allocation forward, write the parquet, then the sequence,
    then the manifest -- so XNSE's tokens stay stable across the source switch
    and the venue's completion record means what it means everywhere else.
    """
    if opts.dry_run:
        print("DRY RUN: Would normalize NSE contract masters")
        return
    if not runner.venue_selected(opts, "XNSE"):
        return
    if not present(opts.date_dir):
        print(f"  No NSE contract drop for XNSE ({drop_dir(opts.date_dir)})")
        return

    exchange_cfg = counter_token.exchange_for("XNSE")
    if exchange_cfg is None or not exchange_cfg.venue_id:
        print("  CRITICAL: skipping XNSE -- no venue_id in conf/config.ini")
        return
    token_errors = counter_token.validate(config.load_exchanges())
    if "XNSE" in token_errors:
        for msg in token_errors["XNSE"]:
            print(f"  CRITICAL [XNSE] counterToken config: {msg}")
        return

    print("  Normalizing NSE contract masters...")
    started_at = counter_token.utc_now()
    rows, counts = rows_for(opts.date_dir)
    for name, n in counts.items():
        print(f"    {name}: {n:,} instrument(s)")
    if not rows:
        print("    No instruments -- nothing written")
        return

    # Positional, within this venue-day, after the filtering above so it has no
    # gaps. NOT joinable across dates or venues -- counterTokenV2 is that key.
    for n, row in enumerate(rows, 1):
        row["counterToken"] = str(n)

    try:
        previous, prev_day = counter_token.opening_tokens(
            opts.date_dir, "XNSE", exchange_cfg.venue_id)
    except ValueError as exc:
        print(f"  CRITICAL: skipping XNSE -- {exc}")
        return

    scripts = [r.get("script", "") for r in rows]
    sequence, seq_from = counter_token.open_sequence(opts.date_dir)
    counter_token.check_capacity("XNSE", sequence.issued, len(scripts))
    tokens = counter_token.carry_forward(
        previous, scripts, exchange_cfg.venue_id, sequence)
    for row in rows:
        row["counterTokenV2"] = tokens.token(row.get("script", ""))

    new_count = len(tokens.assigned) - (
        0 if previous is None
        else len(set(tokens.assigned) & set(previous.assigned)))
    print(f"    XNSE counterTokenV2: {len(tokens.assigned):,} symbol(s), "
          f"{new_count:,} new, {sequence.drawn:,} drawn from the shared "
          f"sequence (now {sequence.issued:,})"
          + (", continuing today's earlier run" if prev_day == opts.date_dir
             else f", carried from {prev_day}" if previous else ", first day")
          + (f", sequence from {seq_from}" if seq_from else ", sequence from 1"))

    output_path = paths.normalized_dir(opts.date_dir) / OUTPUT
    parquet_export.write_rows(output_path, paths.NORMALIZED_COLUMNS, rows)

    # Only after the file exists, and sequence before manifest: a crash between
    # the two leaks numbers rather than letting a re-run reissue live ones.
    counter_token.write_sequence(opts.date_dir, sequence)
    directory = drop_dir(opts.date_dir)
    counter_token.write_venue_manifest(
        opts.date_dir, "XNSE", tokens, started_at=started_at,
        run=counter_token.run_stats(previous, tokens, sequence, prev_day, seq_from),
        inputs=[counter_token.artifact(directory / name, opts.date_dir)
                for name in (CM_FILE, FO_FILE, CD_FILE)],
        outputs=[counter_token.artifact(output_path, opts.date_dir, len(rows))])
    print(f"    Wrote {len(rows):,} rows to {output_path.name}")
