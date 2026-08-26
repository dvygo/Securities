"""counterToken: a per-venue positional counter in a reserved numeric block.

scriptToken carries each source's own instrument id, which is only unique within
that source -- Databento's instrument_id is unique within a dataset, so on
2026-08-12 the raw ids collided 932 times between XCME and XNAS because EQUS ids
start at 1 and run straight into GLBX's low ids. Anything keying on a token
without an exchange column needs something collision-free, and the pg
symbol-master table the plugin pushes to keys on exactly (token, trade_date).

So every venue is numbered into its own reserved block:

    counterToken = base * BLOCK + n,  n counting from 1 inside the block

This is arithmetic, not string concatenation. An earlier version glued the base
digit onto the counter, which made the token's width follow the counter's --
"1"+"35000" and "1"+"1" gave 135000 and 11, the same nominal base at wildly
different magnitudes, with no correct text ordering. Fixed-size blocks give every
token the same width and sort correctly as text or number.

Each venue owns ONE block per column, and filling it raises rather than wrapping:
a wrapped counter would collide inside the venue's own trade_date, which is the
one failure this exists to prevent. The blocks are no longer a table in this
file -- they are config.ini's [EXCHANGE:<MIC>] base_countertoken_integer and
base_countertokenv2_integer, pre-flighted by validate() before normalize runs.

The numbering is positional and therefore per-day -- a contract gets a different
counterToken tomorrow if the universe shifts. That is intended, since the pg
primary key includes trade_date, but it means nothing may join on counterToken
across dates.

int32 budget: six venues x two columns = 12 blocks, bases 1..12, topping out at
1,300,000,000 -- 61% of int32's 2,147,483,647. Bases up to 20 stay inside it
(2,100,000,000), so there is room for four more blocks before a 32-bit consumer
would overflow. validate() refuses anything past MAX_BASE rather than letting it
through to be discovered by an overflowed consumer downstream.
"""
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config, paths


BLOCK = 100_000_000

# Widest signed 32-bit value a downstream consumer can hold. The blocks are sized
# so a token never exceeds it; validate() below refuses a config that would.
INT32_MAX = 2_147_483_647

# Highest base whose entire block still fits int32: base*BLOCK + BLOCK <= INT32_MAX.
MAX_BASE = (INT32_MAX - BLOCK) // BLOCK          # 20


def _blocks(base: int):
    """The block tuple assign() takes, from a single configured base integer.

    One block per base, not two. The old table gave every venue a spill block,
    doubling its ceiling to 200M rows -- but measured usage is ~2% of a single
    block (OPRA 2,002,550 rows on 2026-08-26, the largest venue), and two blocks
    per venue for BOTH v1 and v2 needs 24 blocks, topping out at 2,500,000,000
    and overflowing int32. One block each gives 12 blocks, top 1,300,000,000,
    with the same 100M-row ceiling per venue that has never been approached.
    """
    return (base,)


# venue_id N owns blocks (2N-1, 2N), so the highest venue_id whose v2 block still
# fits int32 is MAX_BASE//2 -- ten venues, against the six allocated today.
MAX_VENUE_ID = MAX_BASE // 2


def validate(exchanges) -> dict:
    """Pre-flight the numbering config. Returns {venue: [error, ...]}.

    Run before any normalizing, so a venue_id whose block would bleed int32 is
    caught while nothing has been written -- not raised from assign() two
    million rows into a file that then has to be thrown away.

    Only venue_id is checked, because it is the only knob: both blocks are
    derived from it (see config.ExchangeCfg.counter_base), so two venues sharing
    a block is unrepresentable rather than merely invalid.

      - venue_id set, positive, and unique across venues
      - venue_id <= MAX_VENUE_ID, i.e. its v2 block stays inside int32
    """
    errors: dict = {}
    seen_ids: dict = {}

    def fail(venue, msg):
        errors.setdefault(venue, []).append(msg)

    for venue in sorted(exchanges):
        cfg = exchanges[venue]
        mic = cfg.venue_name
        vid = cfg.venue_id

        if not vid:
            fail(mic, "venue_id is unset")
            continue
        if vid < 0:
            fail(mic, f"venue_id={vid} is negative")
            continue
        if vid > MAX_VENUE_ID:
            top = (vid * 2) * BLOCK + BLOCK
            fail(mic, f"venue_id={vid} would bleed int32: its counterTokenV2 block "
                      f"(base {vid * 2}) tops out at {top:,} against int32's "
                      f"{INT32_MAX:,}. Highest usable venue_id is {MAX_VENUE_ID}.")
            continue
        if vid in seen_ids:
            fail(mic, f"venue_id {vid} already used by {seen_ids[vid]} -- ids must be "
                      f"unique, they are what the token blocks are derived from")
        else:
            seen_ids[vid] = mic
    return errors


def check_capacity(mic: str, base: int, rows: int) -> None:
    """Raise if `rows` will not fit the block starting at `base`."""
    if rows > BLOCK:
        raise ValueError(
            f"{mic}: {rows:,} rows exceed the {BLOCK:,}-row block at base {base}. "
            f"Allocate a second base -- do not wrap, the tokens would collide."
        )
    top = base * BLOCK + rows
    if top > INT32_MAX:
        raise ValueError(
            f"{mic}: highest token {top:,} exceeds int32 {INT32_MAX:,} "
            f"(base {base}, {rows:,} rows)."
        )


@lru_cache(maxsize=1)
def _exchanges():
    """config.ini's exchange table, read once per process."""
    return config.load_exchanges()


def exchange_for(venue: str):
    """The [EXCHANGE:<MIC>] config for a venue, or None."""
    return _exchanges().get((venue or "").lower())


def bases_for(venue: str, v2: bool = False):
    """Block owned by a venue for counterToken (or counterTokenV2), from config.

    None if the venue has no [EXCHANGE:<MIC>] section or no base configured,
    which is how a non-numbered venue opts out.
    """
    cfg = _exchanges().get((venue or "").lower())
    if cfg is None:
        return None
    base = cfg.counter_base_v2 if v2 else cfg.counter_base
    return _blocks(base) if base else None


def assign(bases, n: int) -> str:
    """counterToken for the n-th row (1-based) of a venue owning `bases` blocks."""
    block_index, offset = divmod(n - 1, BLOCK)
    if block_index >= len(bases):
        raise ValueError(
            f"counterToken blocks exhausted: row {n:,} needs block {block_index + 1} "
            f"but only {len(bases)} are allocated ({bases}). Allocate another base in "
            f"config.ini [EXCHANGE:<MIC>] -- do not wrap, the tokens would collide."
        )
    return str(bases[block_index] * BLOCK + offset + 1)


# ---------------------------------------------------------------------------
# counterTokenV2: the same block arithmetic, with day-to-day memory.
#
# v1 is positional -- row 1 of today's file is token 1, so a contract's token
# moves whenever the universe shifts and nothing may join on it across dates.
# v2 keeps a symbol's number for as long as that symbol keeps appearing:
#
#   1. a script seen before keeps the offset it already had
#   2. a script that stopped appearing releases its offset to the free pool
#   3. a new script takes the lowest free offset, or extends the high-water
#
# Identity is the `script` string within the venue's own block. Reuse is
# immediate -- an offset freed yesterday can be reissued today -- so a symbol
# that vanishes for one day and returns may come back under a different number.
# That is the chosen trade-off; it keeps the block dense.
#
# State is one manifest.json per day, next to that day's normalized output. The
# carry-forward reads the most recent manifest STRICTLY BEFORE the day being
# built, which is what makes a re-run reproducible: rebuilding 2026-08-26 always
# starts from 08-25 regardless of what 08-27 has since done, and weekends and
# holidays are skipped by the walk-back rather than by a calendar.
# ---------------------------------------------------------------------------

# How far back to look for the previous manifest. Long enough for a holiday week
# plus a weekend; past that the venue is treated as new and numbered from
# scratch, which is visible in the log rather than silently continuing a stale
# allocation.
MANIFEST_LOOKBACK_DAYS = 30

MANIFEST_VERSION = 2


@dataclass
class VenueTokens:
    """One venue's counterTokenV2 allocation for one day."""
    venue_id: int
    base: int
    high_water: int = 0                       # highest offset ever handed out
    assigned: Dict[str, int] = field(default_factory=dict)   # script -> offset
    free: List[int] = field(default_factory=list)            # released, ascending

    def token(self, script: str) -> str:
        """Full counterTokenV2 for a script, or "" if it has none."""
        offset = self.assigned.get(script)
        return "" if offset is None else str(self.base * BLOCK + offset)


def carry_forward(
    previous: Optional[VenueTokens], scripts: Sequence[str], venue_id: int, base: int,
) -> VenueTokens:
    """Allocate today's offsets from yesterday's, per the three rules above.

    `scripts` may contain duplicates and any order; only the distinct set
    matters and new ones are taken in sorted order, so the result depends on
    the symbol set alone and not on how the rows happened to arrive. That is
    what makes a re-run byte-identical.
    """
    present = sorted(set(s for s in scripts if s))

    if previous is None:
        assigned = {script: n for n, script in enumerate(present, 1)}
        return VenueTokens(venue_id, base, len(assigned), assigned, [])

    kept = {s: previous.assigned[s] for s in present if s in previous.assigned}
    released = [off for s, off in previous.assigned.items() if s not in kept]
    pool = sorted(set(previous.free) | set(released))

    assigned = dict(kept)
    high_water = previous.high_water
    taken = 0
    for script in present:
        if script in assigned:
            continue
        if taken < len(pool):
            assigned[script] = pool[taken]
            taken += 1
        else:
            high_water += 1
            assigned[script] = high_water
    return VenueTokens(venue_id, base, high_water, assigned, pool[taken:])


def _manifest_path(as_of: str) -> Path:
    return paths.day_dir(as_of) / "manifest.json"


def load_manifest(as_of: str) -> dict:
    """One day's manifest, or {} if it has none / is unreadable."""
    path = _manifest_path(as_of)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"      manifest {path} unreadable ({exc}) -- treating as absent")
        return {}


def previous_tokens(as_of: str, mic: str, venue_id: int) -> tuple[Optional[VenueTokens], str]:
    """Most recent VenueTokens for `mic` strictly before `as_of`, and its date.

    Returns (None, "") when nothing is found inside MANIFEST_LOOKBACK_DAYS, or
    when the stored venue_id disagrees with the configured one. A venue_id
    change moves the venue onto different blocks entirely, so carrying an old
    allocation across it would keep claiming continuity that no longer holds --
    the caller skips the venue instead.
    """
    import datetime as _dt

    day = _dt.datetime.strptime(as_of, "%Y%m%d").date()
    for back in range(1, MANIFEST_LOOKBACK_DAYS + 1):
        stamp = (day - _dt.timedelta(days=back)).strftime("%Y%m%d")
        venues = (load_manifest(stamp) or {}).get("venues") or {}
        entry = venues.get(mic)
        if not entry:
            continue
        stored_id = int(entry.get("venue_id", 0))
        if stored_id != venue_id:
            raise ValueError(
                f"{mic}: venue_id is {venue_id} in config.ini but {stored_id} in "
                f"{stamp}'s manifest. The token blocks are derived from venue_id, "
                f"so this moves the venue onto a different block -- refusing to "
                f"carry the old allocation forward. Restore venue_id={stored_id}, "
                f"or delete the manifests to renumber from scratch."
            )
        return (
            VenueTokens(
                venue_id=stored_id,
                base=int(entry.get("base", 0)),
                high_water=int(entry.get("high_water", 0)),
                assigned={str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
                free=[int(x) for x in (entry.get("free") or [])],
            ),
            stamp,
        )
    return None, ""


def write_manifest(as_of: str, venues: Dict[str, VenueTokens]) -> Path:
    """Write the day's manifest atomically.

    Staged under a PID-scoped name and replaced on success, like every other
    writer here: a half-written manifest read as tomorrow's carry-forward would
    silently re-issue live tokens.
    """
    path = _manifest_path(as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "date": as_of,
        "venues": {
            mic: {
                "venue_id": tokens.venue_id,
                "base": tokens.base,
                "high_water": tokens.high_water,
                "count": len(tokens.assigned),
                "free": tokens.free,
                "assigned": tokens.assigned,
            }
            for mic, tokens in sorted(venues.items())
        },
    }
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    os.replace(staging, path)
    return path


def merge_into_manifest(as_of: str, mic: str, tokens: VenueTokens) -> Path:
    """Add/replace one venue in the day's manifest, keeping the others.

    The Databento and Fyers normalizers run as separate steps over the same day,
    so each merges its own venues rather than rewriting the whole file.
    """
    existing = load_manifest(as_of)
    venues: Dict[str, VenueTokens] = {}
    for name, entry in (existing.get("venues") or {}).items():
        venues[name] = VenueTokens(
            venue_id=int(entry.get("venue_id", 0)),
            base=int(entry.get("base", 0)),
            high_water=int(entry.get("high_water", 0)),
            assigned={str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
            free=[int(x) for x in (entry.get("free") or [])],
        )
    venues[mic] = tokens
    return write_manifest(as_of, venues)
