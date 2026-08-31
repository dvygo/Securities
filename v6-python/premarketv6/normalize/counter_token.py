"""counterToken: one integer sequence, shared by every venue, starting at 1.

scriptToken carries each source's own instrument id, which is only unique within
that source -- Databento's instrument_id is unique within a dataset, so on
2026-08-12 the raw ids collided 932 times between XCME and XNAS because EQUS ids
start at 1 and run straight into GLBX's low ids. Anything keying on a token
without an exchange column needs something collision-free, and the pg
symbol-master table the plugin pushes to keys on exactly (token, trade_date).

The previous answer was a two-digit venue prefix in the token's leading digits.
This one is simpler: a single counter that every venue draws from, so no two
venues can ever be handed the same number and the token is a plain integer.

    XCBO  1 .. 2,006,525
    XCME  2,006,526 .. 2,950,812
    XNAS  2,950,813 .. 2,964,008

The counter is the ONLY thing that is global. Recycling stays per venue: a
script that departs releases its number back into its own venue's pool, and that
venue's next arrival takes it. Only an arrival the pool cannot cover draws a
fresh number from the shared sequence. Keeping the pools separate means a
venue's numbering is still explicable from its own manifest alone, and it is
what keeps the sequence growing slowly -- on the real week XCBO recycled 29,844
numbers, 30% of its arrivals, that the sequence never had to issue.

The sequence carries forward day to day exactly as a venue's allocation does,
through manifests/_sequence.json, found by the same lookback. A day that cannot
find yesterday's sequence starts at 1, which is visible in the log rather than
silently continuing someone else's allocation.

int32 budget: the whole estate now shares 2,147,483,647 rather than each prefix
owning a slice of it. The week's five days across six venues issued 3.0M
numbers. check_capacity() refuses a day that would cross int32 rather than
wrapping, since a wrapped counter would collide with a live token.
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

# The first token ever issued. Not 0: a zero token is indistinguishable from an
# unset integer column downstream.
FIRST_TOKEN = 1


def assign(n: int) -> str:
    """The token for sequence number `n`. The sequence IS the token."""
    if n < FIRST_TOKEN:
        raise ValueError(f"token {n} is below the first issuable token {FIRST_TOKEN}")
    if n > INT32_MAX:
        raise ValueError(
            f"counterToken exhausted: {n:,} crosses int32 ({INT32_MAX:,}). "
            f"Do not wrap -- the token would collide with a live one."
        )
    return str(n)


def capacity() -> int:
    """How many tokens the shared sequence can ever issue."""
    return INT32_MAX - FIRST_TOKEN + 1


def check_capacity(mic: str, issued: int, arriving: int) -> None:
    """Raise if issuing `arriving` more numbers would cross int32."""
    highest = issued + arriving
    if highest > INT32_MAX:
        raise ValueError(
            f"{mic}: {arriving:,} new instrument(s) would take the shared "
            f"sequence to {highest:,}, past int32 ({INT32_MAX:,}). Do not wrap "
            f"-- the tokens would collide with live ones."
        )


def validate(exchanges) -> dict:
    """Pre-flight the numbering config. Returns {venue: [error, ...]}.

    Far less to check than when venue_id was a token prefix. It no longer
    reaches the token at all, so there is no two-digit rule and no adjacent-id
    rule -- venues cannot collide through their ids because they no longer
    number independently. What remains is that a venue_id is set and unique, so
    a manifest can still be held against the venue it claims to describe.
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
        owner = taken.get(vid)
        if owner and owner != mic:
            fail(mic, f"venue_id {vid} already used by {owner}; ids must be unique")
        else:
            taken[vid] = mic
    return errors


MANIFEST_LOOKBACK_DAYS = 30

MANIFEST_VERSION = 3


@dataclass
class VenueTokens:
    """One venue's counterTokenV2 allocation for one day.

    `assigned` holds the token itself now, not an offset into a prefix block --
    there are no blocks any more. `free` holds tokens this venue released and
    may hand to its own next arrival; they are never offered to another venue,
    which is what keeps a venue's numbering explicable from its own file.
    """
    venue_id: int
    assigned: Dict[str, int] = field(default_factory=dict)   # script -> token
    free: List[int] = field(default_factory=list)            # released, ascending

    def token(self, script: str) -> str:
        """Full counterTokenV2 for a script, or "" if it has none."""
        number = self.assigned.get(script)
        return "" if number is None else assign(number)

    @property
    def highest(self) -> int:
        """Highest token this venue holds. 0 when it holds none."""
        return max(self.assigned.values(), default=0)


class Sequence:
    """The shared counter every venue draws new tokens from.

    Deliberately not a per-venue high-water mark: two venues drawing from
    separate counters is exactly the collision this replaced. `issued` is the
    last number handed out to anyone, so the next is issued + 1.
    """

    def __init__(self, issued: int = FIRST_TOKEN - 1):
        self.issued = int(issued)
        self.start = int(issued)

    def take(self) -> int:
        """The next unissued number, or raise rather than cross int32."""
        if self.issued + 1 > INT32_MAX:
            raise ValueError(
                f"counterToken exhausted: the shared sequence has issued "
                f"{self.issued:,} and the next would cross int32 "
                f"({INT32_MAX:,}). Do not wrap -- the token would collide."
            )
        self.issued += 1
        return self.issued

    @property
    def drawn(self) -> int:
        """How many numbers this run has taken."""
        return self.issued - self.start


def carry_forward(
    previous: Optional[VenueTokens], scripts: Sequence, venue_id: int,
    sequence: "Sequence",
) -> VenueTokens:
    """Allocate today's tokens from yesterday's, per the three rules.

    Kept scripts hold their token. Departed scripts release theirs into this
    venue's pool. Arrivals drain that pool first and only then draw a fresh
    number from the shared `sequence` -- draining first is what keeps the
    sequence growing slower than the arrival count.

    `scripts` may contain duplicates and any order; only the distinct set
    matters and new ones are taken in sorted order, so the result depends on
    the symbol set alone and not on how the rows happened to arrive. That is
    what makes a re-run byte-identical.
    """
    present = sorted(set(s for s in scripts if s))

    if previous is None:
        return VenueTokens(venue_id, {s: sequence.take() for s in present}, [])

    kept = {s: previous.assigned[s] for s in present if s in previous.assigned}
    released = [t for s, t in previous.assigned.items() if s not in kept]
    pool = sorted(set(previous.free) | set(released))

    assigned = dict(kept)
    taken = 0
    for script in present:
        if script in assigned:
            continue
        if taken < len(pool):
            assigned[script] = pool[taken]
            taken += 1
        else:
            assigned[script] = sequence.take()
    return VenueTokens(venue_id, assigned, pool[taken:])


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
    """Every venue COMPLETED for a day.

    A venue's manifest is written only after its normalized file is promoted,
    so its presence is the hard answer to "is this venue done for this date".
    That matters because the venues do not arrive together -- GLBX publishes
    definitions around 00:00-01:00Z and OPRA around 10:00-11:00Z, so a day is
    normalized more than once and the early runs legitimately have venues
    missing. Absent means not done yet, never means empty.

    Files beginning with "_" are the day's own bookkeeping (_sequence.json),
    not venues.
    """
    directory = manifests_dir(as_of)
    if not directory.is_dir():
        return set()
    return {p.stem.upper() for p in directory.glob("*.json")
            if not p.name.startswith("_")}


def venue_run(as_of: str, mic: str) -> dict:
    """When a venue's run for a day started and finished, or {}.

    {"started_at": "...Z", "completed_at": "...Z"} -- both UTC ISO8601.
    """
    doc = _read_json(_venue_manifest_path(as_of, mic.upper()))
    return {k: doc[k] for k in ("started_at", "completed_at") if k in doc}


def utc_now() -> str:
    """Timestamp for the run record, to the second, UTC."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokens_from(entry: dict) -> VenueTokens:
    return VenueTokens(
        venue_id=int(entry.get("venue_id", 0)),
        assigned={str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
        free=[int(x) for x in (entry.get("free") or [])],
    )


def write_venue_manifest(as_of: str, mic: str, tokens: VenueTokens,
                         started_at: str = "") -> Path:
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
    completed_at = utc_now()
    payload = {
        "version": MANIFEST_VERSION,
        "date": as_of,
        "venue": mic,
        # The run record. This file existing IS the completion signal, and these
        # say when -- useful when a day was normalized in two passes because
        # GLBX landed at 01:00Z and OPRA at 10:30Z.
        "started_at": started_at or completed_at,
        "completed_at": completed_at,
        "allocation": {
            "venue_id": tokens.venue_id,
            "highest": tokens.highest,
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


def _sequence_path(as_of: str) -> Path:
    """The shared counter for a day, beside that day's venue manifests.

    Underscored so it cannot be mistaken for a venue file by venues_with_manifest,
    which globs the same directory.
    """
    return manifests_dir(as_of) / "_sequence.json"


def load_sequence(as_of: str) -> Optional[int]:
    """The last number issued on a day, or None if the day has no sequence."""
    doc = _read_json(_sequence_path(as_of))
    return int(doc["issued"]) if "issued" in doc else None


def previous_sequence(as_of: str) -> tuple[Optional[int], str]:
    """The most recent sequence strictly before `as_of`, and its date.

    Same lookback the venue allocations use, for the same reason: a gap longer
    than a holiday week means the estate is being numbered fresh, and that
    should be visible in the log rather than silently continuing.
    """
    import datetime as _dt

    day = _dt.datetime.strptime(as_of, "%Y%m%d").date()
    for back in range(1, MANIFEST_LOOKBACK_DAYS + 1):
        stamp = (day - _dt.timedelta(days=back)).strftime("%Y%m%d")
        issued = load_sequence(stamp)
        if issued is not None:
            return issued, stamp
    return None, ""


def open_sequence(as_of: str) -> tuple["Sequence", str]:
    """The sequence to allocate from today, and the day it was carried from.

    Today's own file first, so a second step in the same day (Fyers after
    Databento) continues where the first left off instead of reissuing its
    numbers. Then yesterday's. Then FIRST_TOKEN.
    """
    today = load_sequence(as_of)
    if today is not None:
        return Sequence(today), as_of
    issued, stamp = previous_sequence(as_of)
    if issued is None:
        return Sequence(), ""
    return Sequence(issued), stamp


def write_sequence(as_of: str, sequence: "Sequence") -> Path:
    """Persist the counter, atomically, like every other writer here."""
    path = _sequence_path(as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump({"version": MANIFEST_VERSION, "date": as_of,
                   "issued": sequence.issued}, handle,
                  separators=(",", ":"), sort_keys=True)
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
