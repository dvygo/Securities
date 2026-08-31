"""counterToken: a per-venue counter carrying the venue's id as a prefix.

scriptToken carries each source's own instrument id, which is only unique within
that source -- Databento's instrument_id is unique within a dataset, so on
2026-08-12 the raw ids collided 932 times between XCME and XNAS because EQUS ids
start at 1 and run straight into GLBX's low ids. Anything keying on a token
without an exchange column needs something collision-free, and the pg
symbol-master table the plugin pushes to keys on exactly (token, trade_date).

So the venue's id is the token's leading digits and the counter follows, widening
as it fills:

    prefix 10 -> 1000..1009, 10000..10099, 100000..100999, ...

Prefixes are ALWAYS two digits, 10..99. That is load-bearing, not cosmetic: with
variable-width counters, single-digit prefixes collide the moment a second digit
appears -- prefix 1's three-digit range (100..199) is prefix 10's two-digit range
(100..109). Measured: prefixes 1..12 over 200k tokens each produced 122,220
collisions; 10..30 over 300k each produced none. validate() refuses anything
outside 10..99.

Each venue owns TWO consecutive prefixes: venue_id for counterToken and
venue_id+1 for counterTokenV2. Deriving the second from the first means the two
columns cannot be configured onto the same prefix, which would make them
indistinguishable as integers.

Trade-off accepted knowingly: tokens are no longer fixed-width, so they do not
sort correctly as TEXT ("10" < "100" < "11"). Sort them as numbers. The previous
fixed-size-block scheme sorted either way; this one buys a token that names its
venue in its leading digits instead.

int32 budget: every two-digit prefix reaches a 7-digit counter before crossing
int32's 2,147,483,647, giving 11,111,110 rows per prefix. The largest venue,
OPRA, wrote 2,002,550 rows on 2026-08-26 -- 18% of that. check_capacity()
refuses a day that would not fit rather than wrapping, since a wrapped counter
would collide inside the venue's own trade_date.
"""
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config, paths

# Widest signed 32-bit value a downstream consumer can hold.
INT32_MAX = 2_147_483_647

# Prefixes are two digits. venue_id is the counterToken prefix and venue_id+1 is
# counterTokenV2's, so the highest usable venue_id leaves room for its pair.
MIN_PREFIX = 10
MAX_PREFIX = 99
MAX_VENUE_ID = MAX_PREFIX - 1          # 98, whose pair is 99


def assign(prefix: int, n: int) -> str:
    """Token for the n-th row (1-based) of a venue owning `prefix`.

    The counter widens as each width fills: 10 values at one digit, 100 at two,
    1000 at three. Raises rather than wrapping once the next width would cross
    int32.
    """
    width, remaining = 1, n
    while remaining > 10 ** width:
        remaining -= 10 ** width
        width += 1
        if (prefix + 1) * (10 ** width) - 1 > INT32_MAX:
            raise ValueError(
                f"counterToken exhausted for prefix {prefix}: row {n:,} needs a "
                f"{width}-digit counter, which crosses int32 "
                f"({INT32_MAX:,}). Do not wrap -- the tokens would collide."
            )
    return f"{prefix}{remaining - 1:0{width}d}"


def capacity(prefix: int) -> int:
    """Rows `prefix` can number before a token would exceed int32."""
    total, width = 0, 1
    while (prefix + 1) * (10 ** width) - 1 <= INT32_MAX:
        total += 10 ** width
        width += 1
    return total


def check_capacity(mic: str, prefix: int, rows: int) -> None:
    """Raise if `rows` will not fit under `prefix`."""
    limit = capacity(prefix)
    if rows > limit:
        raise ValueError(
            f"{mic}: {rows:,} rows exceed the {limit:,} that prefix {prefix} can "
            f"number inside int32 ({INT32_MAX:,}). Do not wrap -- the tokens "
            f"would collide inside the venue's own trade_date."
        )


def validate(exchanges) -> dict:
    """Pre-flight the numbering config. Returns {venue: [error, ...]}.

    Run before any normalizing, so a prefix that would bleed int32 or collide is
    caught while nothing has been written.

      - venue_id set and inside 10..98 (its pair, venue_id+1, must stay 2-digit)
      - venue_id unique, and no venue's pair overlapping another's: two venues
        must differ by at least 2, since each owns venue_id and venue_id+1
    """
    errors: dict = {}
    taken: dict = {}

    def fail(venue, msg):
        errors.setdefault(venue, []).append(msg)

    for venue in sorted(exchanges):
        cfg = exchanges[venue]
        mic = cfg.venue_name
        vid = cfg.venue_id

        if not vid:
            fail(mic, "venue_id is unset")
            continue
        if vid < MIN_PREFIX or vid > MAX_VENUE_ID:
            fail(mic, f"venue_id={vid} is outside {MIN_PREFIX}..{MAX_VENUE_ID}. "
                      f"Prefixes must be exactly two digits -- a single-digit "
                      f"prefix collides with a two-digit one as the counter "
                      f"widens (prefix 1's 100..199 is prefix 10's 100..109), "
                      f"and {MAX_PREFIX} is the last whose pair still fits.")
            continue
        for prefix in (vid, vid + 1):
            owner = taken.get(prefix)
            if owner and owner != mic:
                fail(mic, f"prefix {prefix} already owned by {owner}. Each venue "
                          f"takes venue_id and venue_id+1, so ids must differ by "
                          f"at least 2.")
            else:
                taken[prefix] = mic
    return errors


# How far back to look for the previous manifest. Long enough for a holiday week
# plus a weekend; past that the venue is treated as new and numbered from
# scratch, which is visible in the log rather than silently continuing a stale
# allocation.
MANIFEST_LOOKBACK_DAYS = 30

MANIFEST_VERSION = 3


@dataclass
class VenueTokens:
    """One venue's counterTokenV2 allocation for one day."""
    venue_id: int
    prefix: int
    high_water: int = 0                       # highest offset ever handed out
    assigned: Dict[str, int] = field(default_factory=dict)   # script -> offset
    free: List[int] = field(default_factory=list)            # released, ascending

    def token(self, script: str) -> str:
        """Full counterTokenV2 for a script, or "" if it has none."""
        offset = self.assigned.get(script)
        return "" if offset is None else assign(self.prefix, offset)


def carry_forward(
    previous: Optional[VenueTokens], scripts: Sequence[str], venue_id: int, prefix: int,
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
        return VenueTokens(venue_id, prefix, len(assigned), assigned, [])

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
    return VenueTokens(venue_id, prefix, high_water, assigned, pool[taken:])


def manifests_dir(as_of: str) -> Path:
    """data/YYYYMMDD/v6/manifests/ -- one file per venue."""
    return paths.day_dir(as_of) / "manifests"


def _venue_manifest_path(as_of: str, mic: str) -> Path:
    return manifests_dir(as_of) / f"{mic.upper()}.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"      manifest {path} unreadable ({exc}) -- treating as absent")
        return {}


def venue_entry(as_of: str, mic: str) -> dict:
    """One venue's allocation for a day: {venue_id, prefix, high_water, count,
    free, assigned}, or {} when the day has none.

    Per-venue file and nothing else. There is deliberately NO fallback to the
    combined manifest.json this replaced: a silent fallback makes a day whose
    write failed indistinguishable from a day that legitimately had no
    allocation, and those two want opposite responses. A day in the old layout
    now numbers from scratch, visibly in the log, rather than half-chaining to
    a layout nothing maintains.
    """
    doc = _read_json(_venue_manifest_path(as_of, mic.upper()))
    return doc.get("allocation") or {}


def venues_with_manifest(as_of: str) -> set:
    """Every venue that has an allocation for a day."""
    directory = manifests_dir(as_of)
    if not directory.is_dir():
        return set()
    return {p.stem.upper() for p in directory.glob("*.json")}


def _tokens_from(entry: dict) -> VenueTokens:
    return VenueTokens(
        venue_id=int(entry.get("venue_id", 0)),
        prefix=int(entry.get("prefix", 0)),
        high_water=int(entry.get("high_water", 0)),
        assigned={str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
        free=[int(x) for x in (entry.get("free") or [])],
    )


def write_venue_manifest(as_of: str, mic: str, tokens: VenueTokens) -> Path:
    """Write one venue's allocation, atomically, to its own file.

    Replaces merge_into_manifest's read-modify-write of a file holding every
    venue. That pattern had two costs beyond the obvious one: the Databento and
    Fyers steps normalize the same day and each rewrote the whole file, so the
    later one could clobber the earlier if they ever overlapped; and reading one
    venue meant parsing all of them, which on an OPRA week is 85MB to answer a
    question about a venue with 20,000 scripts.

    Staged under a PID-scoped name and replaced on success, as before: a
    half-written manifest read as tomorrow's carry-forward would silently
    re-issue live tokens.
    """
    mic = mic.upper()
    path = _venue_manifest_path(as_of, mic)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "date": as_of,
        "venue": mic,
        "allocation": {
            "venue_id": tokens.venue_id,
            "prefix": tokens.prefix,
            "high_water": tokens.high_water,
            "count": len(tokens.assigned),
            "free": tokens.free,
            "assigned": tokens.assigned,
        },
    }
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    os.replace(staging, path)
    return path


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
        entry = venue_entry(stamp, mic)
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
        return _tokens_from(entry), stamp
    return None, ""


@lru_cache(maxsize=1)
def _exchanges():
    """config.ini's exchange table, read once per process."""
    return config.load_exchanges()


def exchange_for(venue: str):
    """The [EXCHANGE:<MIC>] config for a venue, or None."""
    return _exchanges().get((venue or "").lower())


def prefix_for(venue: str, v2: bool = False):
    """Token prefix a venue owns: venue_id for counterToken, +1 for V2.

    None if the venue has no [EXCHANGE:<MIC>] section or no venue_id, which is
    how a venue opts out of being numbered.
    """
    cfg = exchange_for(venue)
    if cfg is None or not cfg.venue_id:
        return None
    return cfg.venue_id + 1 if v2 else cfg.venue_id
