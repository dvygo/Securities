"""The MDF `.bin` token map: DBN instrument_id -> counterTokenV2, one file per venue.

Emitted straight from the normalized master, replacing the hand-off of a parquet
contract master for a separate tool to convert.

The consumer is C++ (mdfv::TokenMap::Load). It does not read parquet and it
validates strictly: every rule below is a hard startup failure, not a warning, so
this module fails or skips-and-counts rather than writing something that loads.

Layout, little-endian throughout -- a 64-byte header then a packed array of
8-byte entries, no padding and no trailing bytes:

    off  size  field
      0     8  magic "MDFVTOK1", no NUL terminator
      8     4  u32  format version, 1
     12     4  u32  entry count
     16     8  venue code, ASCII NUL-padded to 8
     24     8  u64  source row count      (informational)
     32     8  u64  build timestamp, ns   (informational)
     40     4  u32  min instrument_id     (first entry)
     44     4  u32  max instrument_id     (last entry)
     48     8  u64  FNV-1a over the ENTRY ARRAY only, not the header
     56     8  reserved, zero
    then     8  u32 instrument_id, i32 token   x entry count

The key is the instrument id the LIVE FEED sends for this venue. It is not
unique across venues -- Databento assigns instrument_id per dataset, so XCME
42003239 and XNAS 42003239 are different instruments. That is why there is one
file per venue and why the venue is stamped in the header for the loader to
check against its compiled-in lane.

Getting the key column wrong yields a file that loads cleanly and resolves every
instrument to a plausible token belonging to something else, undetectably. See
key_column() for how it is chosen, and why by precedence rather than by size.
"""
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import config, export, parquet_export, paths, runner

MAGIC = b"MDFVTOK1"
FORMAT_VERSION = 1
HEADER_BYTES = 64
ENTRY_BYTES = 8
VENUE_BYTES = 8

# FNV-1a, 64-bit.
FNV_OFFSET_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
U64_MASK = 0xFFFFFFFFFFFFFFFF

# instrument_id 0 is the hash table's empty-slot marker on the C++ side, so an
# entry carrying it would be invisible to every lookup.
RESERVED_INSTRUMENT_ID = 0

# The token goes on the wire as a positive int32: downstream packet-building
# widens it without sign extension, so 0 and negatives are unusable.
TOKEN_MIN = 1
TOKEN_MAX = 2147483647

U32_MAX = 0xFFFFFFFF

# Where MDF's loader searches. The ini references the bare filename
# (TOB_token_map=tokenmap.XCME.bin), so the name must resolve inside this
# directory with no path of its own.
DELIVERY_DIR = Path("config/cpp-vendor")

# Key column precedence. NOT "whichever has more distinct values": a master where
# def_raw is correct but sparser would silently flip to the wrong column and
# resolve every instrument wrongly, and the file would still load cleanly.
PREFERRED_KEY = "def_raw_instrument_id"
FALLBACK_KEY = "scriptToken"
VALUE_COLUMN = "counterTokenV2"


def filename(venue: str) -> str:
    """tokenmap.<VENUE>.bin -- exactly what the ini references."""
    return f"tokenmap.{venue.upper()}.bin"


def fnv1a(body: bytes) -> int:
    """64-bit FNV-1a over the entry array."""
    h = FNV_OFFSET_BASIS
    for c in body:
        h = ((h ^ c) * FNV_PRIME) & U64_MASK
    return h


class PartiallyPopulated(ValueError):
    """def_raw_instrument_id is neither empty nor whole, so precedence cannot decide.

    Raised rather than guessed: either answer produces a file that loads, and the
    wrong one mislabels every instrument.
    """


def _as_int(raw: str) -> Optional[int]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def key_column(rows: Sequence[dict]) -> str:
    """Which column carries the live instrument id: def_raw_instrument_id, else scriptToken.

    def_raw is preferred whenever it is populated. XCME and XCBO carry it
    byte-identical to scriptToken, so the rule is a no-op there. XNAS is the
    reason the fallback exists: its master has def_raw == "0" on every row and
    only scriptToken populated, and that value lands inside the live id range, so
    it is what the feed actually sends.

    A partially populated def_raw raises rather than picking either column.
    """
    populated = 0
    for row in rows:
        value = _as_int(row.get(PREFERRED_KEY, ""))
        if value:                      # non-zero and parseable
            populated += 1
    if populated == 0:
        return FALLBACK_KEY
    if populated == len(rows):
        return PREFERRED_KEY
    raise PartiallyPopulated(
        f"{PREFERRED_KEY} is populated on {populated:,} of {len(rows):,} row(s). "
        f"Precedence cannot choose between it and {FALLBACK_KEY}, and guessing "
        f"produces a map that loads cleanly while resolving instruments to the "
        f"wrong tokens. Fix the master, or decide the column deliberately."
    )


@dataclass
class Skips:
    """Why rows did not make it into the map. A silent skip shrinks coverage
    without saying so -- the loss surfaces downstream only as tob_untokenized,
    which already carries a large benign baseline from CME user-defined spreads,
    so a real loss would hide in the noise."""
    unparseable_id: int = 0
    unparseable_token: int = 0
    reserved_id: int = 0
    id_out_of_u32: int = 0
    token_out_of_range: int = 0

    def total(self) -> int:
        return (self.unparseable_id + self.unparseable_token + self.reserved_id
                + self.id_out_of_u32 + self.token_out_of_range)

    def describe(self) -> str:
        named = [
            (self.unparseable_id, f"{self.unparseable_id:,} unparseable instrument_id"),
            (self.unparseable_token, f"{self.unparseable_token:,} unparseable token"),
            (self.reserved_id, f"{self.reserved_id:,} instrument_id 0 (reserved)"),
            (self.id_out_of_u32, f"{self.id_out_of_u32:,} instrument_id outside u32"),
            (self.token_out_of_range,
             f"{self.token_out_of_range:,} token outside 1..{TOKEN_MAX:,}"),
        ]
        return ", ".join(text for count, text in named if count) or "none"


class DuplicateInstrumentId(ValueError):
    """Two source rows claim the same instrument_id.

    Fatal rather than deduplicated: the loader requires strictly ascending ids,
    and picking one of the pair would make the map depend on row order while
    hiding an upstream data problem.
    """


@dataclass
class Built:
    venue: str
    entries: List[Tuple[int, int]] = field(default_factory=list)
    source_rows: int = 0
    key: str = ""
    skips: Skips = field(default_factory=Skips)


def build(venue: str, rows: Iterable[dict], key: Optional[str] = None) -> Built:
    """Rows -> sorted, deduplicated entries, with every rejection counted.

    `key` overrides the precedence rule; leave it None to have key_column decide,
    which needs the rows materialized.
    """
    rows = list(rows)
    out = Built(venue=venue.upper(), source_rows=len(rows),
                key=key or key_column(rows))

    seen: Dict[int, int] = {}
    for row in rows:
        instrument_id = _as_int(row.get(out.key, ""))
        if instrument_id is None:
            out.skips.unparseable_id += 1
            continue
        if instrument_id == RESERVED_INSTRUMENT_ID:
            out.skips.reserved_id += 1
            continue
        if not 0 < instrument_id <= U32_MAX:
            out.skips.id_out_of_u32 += 1
            continue

        token = _as_int(row.get(VALUE_COLUMN, ""))
        if token is None:
            out.skips.unparseable_token += 1
            continue
        if not TOKEN_MIN <= token <= TOKEN_MAX:
            out.skips.token_out_of_range += 1
            continue

        if instrument_id in seen and seen[instrument_id] != token:
            raise DuplicateInstrumentId(
                f"{out.venue}: instrument_id {instrument_id} maps to both token "
                f"{seen[instrument_id]} and {token}. The loader requires strictly "
                f"ascending ids, so one of them would have to be dropped -- which "
                f"one wins would depend on row order. Fix the master."
            )
        seen[instrument_id] = token

    out.entries = sorted(seen.items())
    return out


def encode(built: Built, built_at_ns: Optional[int] = None) -> bytes:
    """Serialize to the on-disk layout. Raises if there is nothing to write."""
    if not built.entries:
        raise ValueError(
            f"{built.venue}: no entries survived, and entry_count == 0 is a hard "
            f"load failure. {built.source_rows:,} source row(s), skipped: "
            f"{built.skips.describe()}."
        )

    venue = built.venue.encode("ascii")
    if len(venue) > VENUE_BYTES:
        raise ValueError(f"venue {built.venue!r} exceeds {VENUE_BYTES} ASCII bytes")

    body = b"".join(struct.pack("<Ii", i, t) for i, t in built.entries)

    header = bytearray(HEADER_BYTES)
    header[0:8] = MAGIC
    struct.pack_into("<II", header, 8, FORMAT_VERSION, len(built.entries))
    header[16:24] = venue.ljust(VENUE_BYTES, b"\x00")
    struct.pack_into("<Q", header, 24, built.source_rows)
    struct.pack_into("<Q", header, 32,
                     time.time_ns() if built_at_ns is None else built_at_ns)
    struct.pack_into("<II", header, 40, built.entries[0][0], built.entries[-1][0])
    struct.pack_into("<Q", header, 48, fnv1a(body))
    # bytes 56..64 stay zero -- reserved, and the loader checks it.
    return bytes(header) + body


def verify(blob: bytes, venue: Optional[str] = None) -> Dict[str, object]:
    """Re-read a map the way the loader does. Raises on the first broken invariant.

    Run against this module's own output before delivery: the point is to catch a
    packing mistake here rather than as a lane that will not start.
    """
    if blob[:8] != MAGIC:
        raise ValueError(f"magic is {blob[:8]!r}, expected {MAGIC!r}")
    version, count = struct.unpack_from("<II", blob, 8)
    if version != FORMAT_VERSION:
        raise ValueError(f"format version {version}, expected {FORMAT_VERSION}")
    stamped = blob[16:24].rstrip(b"\x00").decode("ascii")
    if venue is not None and stamped != venue.upper():
        raise ValueError(f"venue {stamped!r}, expected {venue.upper()!r}")
    if blob[56:64] != b"\x00" * VENUE_BYTES:
        raise ValueError("reserved bytes 56..64 are not zero")
    expected = HEADER_BYTES + count * ENTRY_BYTES
    if len(blob) != expected:
        raise ValueError(f"file is {len(blob)} bytes, expected exactly {expected} "
                         f"(header + {count} entries) -- trailing bytes are a load failure")
    if count == 0:
        raise ValueError("entry_count is 0")

    lo, hi = struct.unpack_from("<II", blob, 40)
    checksum, = struct.unpack_from("<Q", blob, 48)
    if fnv1a(blob[HEADER_BYTES:]) != checksum:
        raise ValueError("FNV-1a mismatch over the entry array")

    previous = None
    for index in range(count):
        instrument_id, token = struct.unpack_from("<Ii", blob, HEADER_BYTES + index * ENTRY_BYTES)
        if instrument_id == RESERVED_INSTRUMENT_ID:
            raise ValueError(f"entry {index}: instrument_id 0 is reserved")
        if not TOKEN_MIN <= token <= TOKEN_MAX:
            raise ValueError(f"entry {index}: token {token} outside {TOKEN_MIN}..{TOKEN_MAX}")
        if previous is not None and instrument_id <= previous:
            raise ValueError(f"entry {index}: instrument_id {instrument_id} not strictly "
                             f"greater than {previous}")
        previous = instrument_id
        if index == 0 and instrument_id != lo:
            raise ValueError(f"header min {lo} disagrees with first entry {instrument_id}")
    if previous != hi:
        raise ValueError(f"header max {hi} disagrees with last entry {previous}")

    source_rows, = struct.unpack_from("<Q", blob, 24)
    built_at_ns, = struct.unpack_from("<Q", blob, 32)
    return {"venue": stamped, "entries": count, "min_id": lo, "max_id": hi,
            "bytes": len(blob), "source_rows": source_rows, "built_at_ns": built_at_ns}


def write(path: Path, blob: bytes) -> None:
    """Write atomically -- temp file then rename.

    A lane restarting mid-delivery must not read a half-written map. The size and
    checksum checks would catch it, but the failure mode is a lane that refuses
    to start, which is worth not causing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(temp, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def tokenmap_dir(date_dir: str) -> Path:
    """Where maps land inside the day's tree, before delivery:
    data/YYYYMMDD/v6/plugin/tokenmap/ -- under plugin/, because that is where
    output in someone else's format lives."""
    return paths.plugin_dir(date_dir) / "tokenmap"


def run(opts: runner.Opts) -> None:
    """Emit tokenmap.<VENUE>.bin for every Databento venue with a normalized master.

    Only Databento venues: the key is the instrument id the DBN feed sends, and
    the Fyers/NSE venues have no such id -- their scriptToken is the vendor's own
    instrument number, which no MDF lane will ever look up.
    """
    if opts.dry_run:
        print("DRY RUN: Would build MDF token maps")
        return

    print("  Building MDF token maps...")
    normalized = export.normalized_files(opts.date_dir)
    if not normalized:
        print("    No normalized files found")
        return

    exchanges = config.load_exchanges()
    out_dir = Path(opts.tokenmap_dir) if getattr(opts, "tokenmap_dir", None) else tokenmap_dir(opts.date_dir)
    written = 0

    for src_path in sorted(normalized):
        venue = src_path.name.split("-", 1)[0]
        if not runner.venue_selected(opts, venue):
            continue
        venue_cfg = exchanges.get(venue.lower())
        if venue_cfg is None or not venue_cfg.enabled:
            print(f"    Skipping {venue}: enabled = 0")
            continue
        if venue_cfg.feed != "databento":
            # Not a lapse -- see the docstring. Said out loud so a missing file
            # is never mistaken for a failed build.
            print(f"    Skipping {venue}: feed = {venue_cfg.feed}, no DBN instrument_id")
            continue

        rows = list(parquet_export.read_rows(src_path))
        try:
            built = build(venue, rows)
            blob = encode(built)
            info = verify(blob, venue)
        except (PartiallyPopulated, DuplicateInstrumentId, ValueError) as exc:
            print(f"    CRITICAL [{venue}] token map not written -- {exc}")
            continue

        path = out_dir / filename(venue)
        write(path, blob)
        written += 1
        print(f"    {venue}: {built.source_rows:,} source row(s) -> "
              f"{info['entries']:,} entries via {built.key}, ids "
              f"{info['min_id']:,}..{info['max_id']:,}, {info['bytes']:,} bytes")
        print(f"      skipped: {built.skips.describe()}")
        print(f"      wrote {path}")

    if written:
        print(f"    {written} map(s) in {out_dir}")
        print(f"    deliver into MDF's {DELIVERY_DIR}/ -- the ini references the "
              f"bare filename, so it must resolve there with no path")
