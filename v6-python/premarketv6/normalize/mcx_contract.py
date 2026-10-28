"""MCX's own contract file -> the canonical schema, for XIMC.

The BOD process places one zip a day in

    data/YYYYMMDD/MCX/<anything>.zip
        MCXScrips.bcp                 every contract        -> read
        MCX_ASSET_MASTER.bcp          asset id -> name      -> ignored
        MCX_ASSET_UNDERLYING_MAP.bcp  underlying id -> asset -> ignored
        MCX_PART.bcp                  participant (broker) list -> ignored

and this reads MCXScrips.bcp straight out of the zip; nothing is extracted. The
folder is named MCX, not XIMC, because that is what the BOD process creates.

MCXScrips.bcp is comma-separated with no header row, 118 columns. The column
meanings below were worked out from the 2026-10-28 drop (file dated 2026-10-15,
14,195 rows) and checked against the whole file; they are not from a published
spec. What cost effort to establish:

DATES ARE PLAIN UNIX SECONDS. Unlike NSE's files there is no 1980 epoch: every
dated contract expires on or after the file's own date (column 1). Expiry
(column 19) is the expiry DAY at 23:59:59 UTC; only the date is used, and the
time is set from the commodity group's session close (below), as the NSE reader
does for India.

PRICES ARE IN PAISE. Strike (column 55) and tick (column 21) arrive x100 and stay
that way -- multiplier is 100, the price scale, as for NSE. 16600000 is a
Rs 1,66,000 strike; a GOLD future's tick is 100 (Rs 1), a GOLD option's 50.

LOT SIZE IS COLUMN 20, in the physical unit in column 76: GOLD 1 KGS, GOLDM 100
GRMS, SILVER 30 KGS, CRUDEOIL 100 BBL, ZINC 5 MT. Checked against MCX's own lot
sizes for 18 products. Column 79 looks like it and is not: it is 0 for every
energy contract.

AN OPTION IS NAMED BY ITS OWN EXPIRY MONTH, NOT COLUMN 104. Column 104 is the
contract month of the underlying FUTURE, and one future can carry two option
series (GOLD DEC2026 has options expiring 30 Oct and 27 Nov), so naming by it
repeats 10,132 times. The option's own expiry month gives one name per contract:
GOLD26OCT166000CE. The name carries no "MCX:" prefix, at the owner's request.

CALENDAR SPREADS ARE DROPPED. Six rows typed FUTCOM are spreads ("GOLD DEC FEB
2027", symbol GLDDECFE27, tokens above 132 million); they are the only FUTCOM
rows with column 95 = "0", and one of them is listed twice.

THE TOKEN IS MCX'S OWN. counterTokenV2 is column 5 unchanged, so XIMC is not
numbered in data/_state: no plan, no reservation, no manifest. That was the
owner's call for now; it can collide with another venue's counterTokenV2 in a
shared resultset, which is why nothing pushes there yet.

TRADING HOURS COME FROM THE COMMODITY GROUP (column 43), confirmed by the
owner, and move with US daylight saving the way MCX's evening session does:

    metals, energy, indices, plastics   09:00-23:30 IST, 23:55 while the US is
                                        on standard time
    agri                                09:00-17:00 IST
    COTTON, CPO, KAPAS, MENTHAOIL       09:00-21:00 IST, 21:30 while the US is
                                        on standard time
"""
import csv
import datetime as dt
import io
import zipfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .. import paths, parquet_export, runner, config
from . import broker_script, flags, fields as fyers_fields

MIC = "XIMC"
FEED = "mcx"
DROP_DIR = "MCX"
MEMBER = "MCXScrips.bcp"
OUTPUT = "XIMC-MCX.parquet"
WIDTH = 118
SCALE = 100

COL_FILE_TS = 1
COL_TOKEN = 5
COL_SYMBOL = 6
COL_EXPIRY = 19
COL_LOT = 20
COL_TICK = 21
COL_DESC = 39
COL_GROUP = 43
COL_INST = 53
COL_STRIKE = 55
COL_OPTION = 56
COL_PRODUCT_CODE = 95
COL_CURRENCY = 102

INSTRUMENT_TYPES = ("FUTCOM", "OPTFUT", "FUTIDX", "OPTIDX", "COM")

# IST opening, and the IST close with the US on daylight time and on standard
# time. IST has no daylight saving, so the UTC opening never moves.
METALS_ENERGY, AGRI, INTL_AGRI = "METALS_ENERGY", "AGRI", "INTL_AGRI"
PROFILES = {
    METALS_ENERGY: {"open": (9, 0), "close_us_dst": (23, 30), "close_us_std": (23, 55)},
    AGRI:          {"open": (9, 0), "close_us_dst": (17, 0),  "close_us_std": (17, 0)},
    INTL_AGRI:     {"open": (9, 0), "close_us_dst": (21, 0),  "close_us_std": (21, 30)},
}
METALS_ENERGY_GROUPS = {
    "PRECIOUS_METAL", "NON_FERROUS_METAL", "FERROUS_METAL",
    "ENERGY_PRODUCTS", "INDICES", "PLASTICS",
}
AGRI_GROUPS = {
    "AGRO_PRODUCTS", "FOOD_GRAINS", "JAGGERY", "OIL", "OIL_SEEDS", "OIL_SEED_MEALS",
    "PLANTATION", "SPICES", "VEGETABLE", "FIBRE", "OTHERS", "WEATHER",
}
INTL_AGRI_SYMBOLS = {"COTTON", "CPO", "KAPAS", "MENTHAOIL"}

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
US_EASTERN = ZoneInfo("America/New_York")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class DropError(ValueError):
    """The day's MCX drop cannot be read as it stands. Nothing was written."""


def drop_dir(as_of: str) -> Path:
    """data/YYYYMMDD/MCX/, where the BOD process places the zip."""
    return paths.venue_dir(as_of, DROP_DIR)


def find_zip(as_of: str) -> Optional[Path]:
    """The day's zip; None when there is none. More than one is refused: which
    one is today's is not something to guess."""
    directory = drop_dir(as_of)
    if not directory.is_dir():
        return None
    zips = sorted(p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() == ".zip")
    if not zips:
        return None
    if len(zips) > 1:
        raise DropError(f"{len(zips)} zip files in {directory} "
                        f"({', '.join(p.name for p in zips)}); leave exactly one")
    return zips[0]


def read_member(zip_path: Path) -> List[List[str]]:
    """MCXScrips.bcp's rows, read from inside the zip, every cell stripped."""
    with zipfile.ZipFile(zip_path) as archive:
        names = [n for n in archive.namelist()
                 if Path(n).name.lower() == MEMBER.lower()]
        if not names:
            raise DropError(f"{zip_path.name} has no {MEMBER} "
                            f"(it holds: {', '.join(archive.namelist())})")
        raw = archive.read(names[0])
    text = io.StringIO(raw.decode("latin-1"))
    return [[cell.strip() for cell in row] for row in csv.reader(text) if row]


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _utc_date(unix_seconds: int) -> Optional[dt.date]:
    if unix_seconds <= 0:
        return None
    return dt.datetime.fromtimestamp(unix_seconds, tz=dt.timezone.utc).date()


def us_on_standard_time(day: dt.date) -> bool:
    """Whether US clocks are on standard (winter) time on `day`.

    MCX moves its evening close when the US changes clocks; the change falls on
    a Sunday, so the first trading day after it is already on the new hours.
    """
    noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=US_EASTERN)
    return not noon.dst()


def profile_for(group: str, symbol: str) -> Optional[str]:
    """The session profile for a row; None for a group not in the table."""
    if symbol in INTL_AGRI_SYMBOLS:
        return INTL_AGRI
    if group in METALS_ENERGY_GROUPS:
        return METALS_ENERGY
    if group in AGRI_GROUPS:
        return AGRI
    return None


def _ist_to_utc(day: dt.date, hm: Tuple[int, int]) -> dt.datetime:
    local = dt.datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=IST)
    return local.astimezone(dt.timezone.utc)


def _close(profile: str, day: dt.date) -> Tuple[int, int]:
    conf = PROFILES[profile]
    return conf["close_us_std"] if us_on_standard_time(day) else conf["close_us_dst"]


def session_utc(profile: Optional[str], day: dt.date) -> str:
    """The trading session on `day` as "HHMM-HHMM" in UTC; "" when unknown."""
    if profile is None:
        return ""
    start = _ist_to_utc(day, PROFILES[profile]["open"])
    end = _ist_to_utc(day, _close(profile, day))
    return f"{start:%H%M}-{end:%H%M}"


def expiration_ns(expiry_unix: int, profile: Optional[str]) -> int:
    """Expiry as epoch nanoseconds at the session close of the expiry day.

    The file's own 23:59:59 UTC is only a date marker. A group outside the table
    keeps it, since there is no close to put in its place.
    """
    day = _utc_date(expiry_unix)
    if day is None:
        return 0
    if profile is None:
        return expiry_unix * 1_000_000_000
    return int(_ist_to_utc(day, _close(profile, day)).timestamp()) * 1_000_000_000


def strike_text(paise: int) -> str:
    """A strike in rupees for names and descriptions: 16600000 -> "166000"."""
    if paise <= 0:
        return ""
    text = f"{paise / SCALE:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _human_date(day: Optional[dt.date]) -> str:
    """"30 Oct 26", the form the NSE reader uses in scriptDetails."""
    if day is None:
        return ""
    return f"{day.day:02d} {MONTHS[day.month - 1]} {day.year % 100:02d}"


def script_for(row: List[str]) -> str:
    """GOLD26OCT166000CE, GOLD27AUGFUT, or GOLD for a spot row."""
    symbol, inst = row[COL_SYMBOL], row[COL_INST]
    if inst == "COM":
        return symbol
    day = _utc_date(_int(row[COL_EXPIRY]))
    yymon = f"{day.year % 100:02d}{MONTHS[day.month - 1].upper()}" if day else ""
    if inst.startswith("OPT"):
        return f"{symbol}{yymon}{strike_text(_int(row[COL_STRIKE]))}{row[COL_OPTION]}"
    return f"{symbol}{yymon}FUT"


def is_spread(row: List[str]) -> bool:
    """A calendar spread: FUTCOM with no product code."""
    return row[COL_INST] == "FUTCOM" and row[COL_PRODUCT_CODE] == "0"


def map_row(row: List[str], as_of_day: dt.date) -> Dict[str, object]:
    """One MCXScrips.bcp row -> the canonical schema."""
    symbol, inst = row[COL_SYMBOL], row[COL_INST]
    option = row[COL_OPTION].upper()
    expiry_unix = _int(row[COL_EXPIRY]) if inst != "COM" else 0
    day = _utc_date(expiry_unix)
    profile = profile_for(row[COL_GROUP], symbol)
    strike = _int(row[COL_STRIKE]) if inst.startswith("OPT") else 0
    script = script_for(row)

    if inst == "COM":
        details = row[COL_DESC] or symbol
    else:
        parts = [symbol, _human_date(day)]
        parts += [strike_text(strike), option] if inst.startswith("OPT") else ["FUT"]
        details = " ".join(p for p in parts if p)

    out: Dict[str, object] = {
        "script": script,
        "scriptToken": row[COL_TOKEN],
        "scriptDetails": details,
        "scriptInstrumentType": inst,
        "scriptInstrumentType2": fyers_fields.instrument_type2(inst),
        "ISIN": "",
        "multiplier": SCALE,
        "lotSize": _int(row[COL_LOT], 1) or 1,
        "tickSize": _int(row[COL_TICK], 0),
        "tradingSessionUTC": session_utc(profile, as_of_day),
        "expiration": expiration_ns(expiry_unix, profile),
        "underlying": symbol,
        "underlying_root": symbol,
        "strike": strike if strike > 0 else 0,
        "optionType": "CALL" if option == "CE" else ("PUT" if option == "PE" else ""),
        "currency": row[COL_CURRENCY] or "INR",
    }
    out["brokerScript1"] = broker_script.from_equity(script)
    broker_script.fill_unspecified(out)
    flags.fill(out)
    return out


def rows_for(as_of: str, zip_path: Path) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    """Every live contract in the day's drop, and what was dropped and why."""
    as_of_day = dt.datetime.strptime(as_of, "%Y%m%d").date()
    raw = read_member(zip_path)
    stats: Dict[str, object] = {"read": len(raw), "malformed": 0, "spreads": 0,
                                "expired": 0, "unknown_type": {}, "unknown_group": {},
                                "collisions": 0, "file_date": None}

    rows: List[Dict[str, object]] = []
    seen: Dict[str, int] = {}
    for row in raw:
        if len(row) != WIDTH:
            stats["malformed"] += 1
            continue
        if stats["file_date"] is None:
            stats["file_date"] = _utc_date(_int(row[COL_FILE_TS]))
        inst = row[COL_INST]
        if inst not in INSTRUMENT_TYPES:
            stats["unknown_type"][inst] = stats["unknown_type"].get(inst, 0) + 1
            continue
        if is_spread(row):
            stats["spreads"] += 1
            continue
        if inst != "COM":
            day = _utc_date(_int(row[COL_EXPIRY]))
            if day is None or day < as_of_day:
                stats["expired"] += 1
                continue
        if profile_for(row[COL_GROUP], row[COL_SYMBOL]) is None:
            group = row[COL_GROUP]
            stats["unknown_group"][group] = stats["unknown_group"].get(group, 0) + 1
        mapped = map_row(row, as_of_day)
        # counterTokenV2 maps script -> token, so a second row with the same
        # script would break one-to-one. The first wins and the rest are counted.
        if mapped["script"] in seen:
            stats["collisions"] += 1
            continue
        seen[mapped["script"]] = 1
        rows.append(mapped)
    return rows, stats


def _report(stats: Dict[str, object], kept: int, as_of: str) -> None:
    print(f"    {MEMBER}: {stats['read']:,} row(s) read, {kept:,} kept")
    file_date = stats["file_date"]
    as_of_day = dt.datetime.strptime(as_of, "%Y%m%d").date()
    if file_date is not None and file_date != as_of_day:
        age = (as_of_day - file_date).days
        when = f"{age} day(s) before" if age > 0 else f"{-age} day(s) after"
        print(f"    WARNING: {MEMBER} is dated {file_date.isoformat()}, {when} {as_of}")
    for key, label in (("expired", "expired before the run date"),
                       ("spreads", "calendar spreads"),
                       ("malformed", f"rows not {WIDTH} columns wide"),
                       ("collisions", "a script already claimed by an earlier row")):
        if stats[key]:
            print(f"    {stats[key]:,} dropped: {label}")
    for key, label in (("unknown_type", "dropped, instrument type not handled"),
                       ("unknown_group", "kept, commodity group not in the session table "
                                         "(blank session, the file's own expiry time)")):
        found = stats[key]
        if found:
            print(f"    {sum(found.values()):,} {label}: "
                  + ", ".join(f"{k or '(blank)'}={n:,}" for k, n in sorted(found.items())))


def run(opts: runner.Opts) -> None:
    """Normalize step: the XIMC venue, from MCX's own contract file.

    Files only, and no numbering: counterTokenV2 is MCX's token (see the module
    notes), so there is no plan, reservation or manifest for XIMC.
    """
    if not config.owns(MIC, FEED):
        print(f"  Skipping MCX contract file: {MIC} feed = {config.feed_for(MIC)}")
        return
    if not runner.venue_selected(opts, MIC):
        return
    try:
        zip_path = find_zip(opts.date_dir)
    except DropError as exc:
        print(f"  CRITICAL: skipping {MIC} -- {exc}")
        return
    if zip_path is None:
        print(f"  No MCX contract zip for {MIC} ({drop_dir(opts.date_dir)})")
        return

    print(f"  Normalizing MCX contract file ({zip_path.name})...")
    try:
        rows, stats = rows_for(opts.date_dir, zip_path)
    except (DropError, zipfile.BadZipFile) as exc:
        print(f"  CRITICAL: skipping {MIC} -- {exc}")
        return
    _report(stats, len(rows), opts.date_dir)
    if not rows:
        print("    No live contracts -- nothing written")
        return

    # Positional within this venue-day, as for every venue; not a join key.
    for n, row in enumerate(rows, 1):
        row["counterToken"] = str(n)
        row["counterTokenV2"] = row["scriptToken"]

    if opts.dry_run:
        print(f"    DRY RUN: would write {len(rows):,} rows to {OUTPUT}")
        return
    output_path = paths.normalized_dir(opts.date_dir) / OUTPUT
    parquet_export.write_rows(output_path, paths.NORMALIZED_COLUMNS, rows)
    print(f"    Wrote {len(rows):,} rows to {output_path.name}")
