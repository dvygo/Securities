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
import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

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

# 4 moved the allocation out of the header and into a Parquet table beside
# it. There is no reader for 3 -- see venue_entry on why no fallback.
MANIFEST_VERSION = 4

# Matches the rest of the pipeline's Parquet. The table is mostly a sorted
# string column and a near-dense integer one, which zstd takes to very little.
_ALLOC_COMPRESSION = "zstd"


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


# The allocation table's filename, beside the header that describes it.
ALLOC_SUFFIX = ".alloc.parquet"

# The two states a token in the table can be in. Every token a venue owns is in
# exactly one of them.
ALLOC_ASSIGNED = "assigned"
ALLOC_FREE = "free"


class ManifestCorrupt(ValueError):
    """A header exists but its allocation table is missing or does not hash.

    Deliberately a ValueError, so the `except ValueError` the normalizers
    already wrap previous_tokens in catches it and skips the venue. Skipping is
    the only safe response. The alternative -- treating a corrupt manifest as
    absent, which is what an unreadable file used to do -- would renumber a live
    venue from scratch and hand its tokens to different instruments.
    """


def manifests_dir(as_of: str) -> Path:
    """data/YYYYMMDD/v6/manifests/ -- a header and a table per venue."""
    return paths.day_dir(as_of) / "manifests"


def _venue_manifest_path(as_of: str, mic: str) -> Path:
    """The venue's header: small, JSON, and the completion record for the day."""
    return manifests_dir(as_of) / f"{mic.upper()}.json"


def _alloc_path(as_of: str, mic: str) -> Path:
    """The venue's allocation table: every token it holds, assigned or free."""
    return manifests_dir(as_of) / f"{mic.upper()}{ALLOC_SUFFIX}"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"      manifest {path} unreadable ({exc}) -- treating as absent")
        return {}


def sha256_of(path: Path) -> str:
    """Digest of a finished file, streamed so a large one costs no memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def build_sha() -> str:
    """Which build produced a manifest.

    Stamped into every header because the numbering is only reproducible against
    the code that wrote it, and nothing in the output distinguishes two builds:
    counterTokenV3 was removed and the whole week re-normalized on 2026-09-01,
    and the files from before and after are indistinguishable without this.

    PREMARKET_BUILD_SHA wins, so a frozen binary can carry its own stamp with no
    git present. Otherwise the working tree's HEAD. Otherwise "unknown", which
    is recorded as such rather than omitted -- a missing key reads as an older
    manifest, an explicit "unknown" reads as what it is.
    """
    stamped = os.environ.get("PREMARKET_BUILD_SHA", "").strip()
    if stamped:
        return stamped
    import subprocess
    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def _alloc_schema():
    """script is null exactly when the token is free; state says which."""
    return pa.schema([
        ("script", pa.string()),
        ("token", pa.int32()),
        ("state", pa.string()),
    ])


def write_alloc(path: Path, tokens: VenueTokens) -> Path:
    """The venue's whole token holding, as one Parquet table.

    Replaces the {script: token} map that used to sit inside the header. On an
    OPRA day that map was 64 MB of JSON and 97% of the file, and every re-run of
    a day rewrote all of it -- under the object versioning that WORM storage
    forces on, that is another undeleteable 64 MB copy per run, and this day is
    normalized at least twice because GLBX lands at 01:00Z and OPRA at 10:30Z.
    Dictionary-encoded columns take the same content to a fraction of it, and a
    downstream consumer can query the table directly instead of parsing our JSON.

    Assigned and free live in ONE file on purpose. They are two halves of a
    single invariant -- every token this venue owns is in exactly one of them --
    and splitting them would let a crash land between the two writes, leaving an
    allocation with an empty pool. Tomorrow's carry_forward reads that as
    "nothing to recycle" and draws fresh numbers for arrivals that had perfectly
    good ones waiting. One file cannot tear that way.

    Row order is canonical (assigned by script, then free ascending) so that
    re-running a day is byte-identical. That is what makes the header's sha256
    worth recording: a digest over a nondeterministic file proves nothing.
    """
    scripts = sorted(tokens.assigned)
    free = sorted(set(tokens.free))
    table = pa.Table.from_arrays(
        [
            pa.array(scripts + [None] * len(free), pa.string()),
            pa.array([tokens.assigned[s] for s in scripts] + free, pa.int32()),
            pa.array([ALLOC_ASSIGNED] * len(scripts) + [ALLOC_FREE] * len(free),
                     pa.string()),
        ],
        schema=_alloc_schema(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    pq.write_table(table, staging, compression=_ALLOC_COMPRESSION)
    os.replace(staging, path)
    return path


def read_alloc(path: Path) -> tuple:
    """(assigned, free) from an allocation table.

    `state` is redundant with "script is null" on purpose, and this is where the
    redundancy pays: the two are checked against each other, so a table that was
    rewritten by something that did not understand the convention is caught here
    rather than silently losing a venue's free pool.
    """
    table = pq.read_table(path, columns=["script", "token", "state"])
    assigned = {}
    free = []
    for script, token, state in zip(table.column("script").to_pylist(),
                                    table.column("token").to_pylist(),
                                    table.column("state").to_pylist()):
        if state == ALLOC_FREE:
            if script:
                raise ManifestCorrupt(
                    f"{path.name}: token {token} is free but names script {script!r}")
            free.append(int(token))
        elif state == ALLOC_ASSIGNED:
            if not script:
                raise ManifestCorrupt(
                    f"{path.name}: token {token} is assigned but names no script")
            assigned[str(script)] = int(token)
        else:
            raise ManifestCorrupt(
                f"{path.name}: token {token} has unknown state {state!r}")
    return assigned, free


def venue_entry(as_of: str, mic: str) -> dict:
    """One venue's allocation for a day: {venue_id, highest, count, free,
    assigned}, or {} when the day has none.

    The header answers "is this venue done"; the table beside it carries the
    tokens. They are read as a pair deliberately -- the header records the
    table's sha256, so a torn, truncated or edited table is caught here instead
    of propagating into tomorrow's numbering.

    Per-venue files and nothing else. There is deliberately NO fallback: not to
    the combined manifest.json this replaced, and not to the v3 header that
    carried its allocation inline. A silent fallback makes a day whose write
    failed indistinguishable from a day that legitimately had no allocation, and
    those two want opposite responses. A day in an older layout numbers from
    scratch, visibly in the log, rather than half-chaining to a layout nothing
    maintains.
    """
    mic = mic.upper()
    doc = _read_json(_venue_manifest_path(as_of, mic))
    if not doc:
        return {}
    block = doc.get("allocation") or {}
    path = _alloc_path(as_of, mic)
    if not path.exists():
        raise ManifestCorrupt(
            f"{mic}: {as_of} has a header but no allocation table ({path.name}). "
            f"Refusing to read this as an unnumbered venue -- that would renumber "
            f"it from scratch and hand its live tokens to other instruments.")
    recorded = str(block.get("sha256", ""))
    actual = sha256_of(path)
    if recorded and recorded != actual:
        raise ManifestCorrupt(
            f"{mic}: {as_of}'s allocation table does not match its header -- "
            f"sha256 is {actual} on disk but the header records {recorded}. The "
            f"table changed after the manifest was written.")
    assigned, free = read_alloc(path)
    return {
        "venue_id": int(block.get("venue_id", 0)),
        "highest": int(block.get("highest", 0)),
        "count": int(block.get("count", len(assigned))),
        "assigned": assigned,
        "free": free,
    }


def venues_with_manifest(as_of: str) -> set:
    """Every venue COMPLETED for a day.

    A venue's header is written only after its normalized file is promoted and
    its allocation table is on disk, so its presence is the hard answer to "is
    this venue done for this date". That matters because the venues do not
    arrive together -- GLBX publishes definitions around 00:00-01:00Z and OPRA
    around 10:00-11:00Z, so a day is normalized more than once and the early
    runs legitimately have venues missing. Absent means not done yet, never
    means empty.

    Files beginning with "_" are the day's own bookkeeping (_sequence.json), not
    venues. Allocation tables are not .json and so cannot be mistaken for one.
    """
    directory = manifests_dir(as_of)
    if not directory.is_dir():
        return set()
    return {p.stem.upper() for p in directory.glob("*.json")
            if not p.name.startswith("_")}


def venue_run(as_of: str, mic: str) -> dict:
    """The run record for a venue-day, or {}.

    started_at/completed_at are UTC ISO8601. build_sha names the code, and the
    tokens block is what the numbering did -- see RunStats.
    """
    doc = _read_json(_venue_manifest_path(as_of, mic.upper()))
    if not doc:
        return {}
    run = {k: doc[k] for k in ("started_at", "completed_at") if k in doc}
    code = doc.get("code") or {}
    if code:
        run["build_sha"] = code.get("build_sha", "")
    if doc.get("tokens"):
        run["tokens"] = doc["tokens"]
    return run


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


@dataclass
class RunStats:
    """What one venue-day's numbering actually did.

    Recorded in the header because it is the recycling audit trail, and until
    now it existed only in a line of stdout. "0 drawn from the shared sequence"
    is the entire claim of the design -- that arrivals are served from the
    venue's own pool before the counter moves -- and a handover needs that in
    the artefact, not in a log someone has to still have.

    Derivable by diffing two days' tables, but only if both days survive. This
    makes a single header self-describing.
    """
    arrived: int = 0
    departed: int = 0
    kept: int = 0
    reused: int = 0
    released: int = 0
    drawn: int = 0
    sequence_before: int = 0
    sequence_after: int = 0
    carried_from: str = ""
    sequence_from: str = ""

    def as_dict(self) -> dict:
        return {
            "arrived": self.arrived, "departed": self.departed,
            "kept": self.kept, "reused": self.reused,
            "released": self.released, "drawn": self.drawn,
            "sequence_before": self.sequence_before,
            "sequence_after": self.sequence_after,
            "carried_from": self.carried_from,
            "sequence_from": self.sequence_from,
        }


def run_stats(previous, tokens: VenueTokens, sequence,
              carried_from: str = "", sequence_from: str = "") -> RunStats:
    """Read off what carry_forward just did, from its own inputs and output.

    Computed here rather than in each normalizer so the Databento and Fyers
    paths cannot drift on what "reused" means.
    """
    before = sequence.start if sequence is not None else 0
    after = sequence.issued if sequence is not None else 0
    drawn = after - before
    if previous is None:
        return RunStats(arrived=len(tokens.assigned), kept=0, reused=0,
                        drawn=drawn, sequence_before=before, sequence_after=after,
                        carried_from=carried_from, sequence_from=sequence_from)
    kept = set(tokens.assigned) & set(previous.assigned)
    arrived = len(tokens.assigned) - len(kept)
    return RunStats(
        arrived=arrived,
        departed=len(set(previous.assigned) - kept),
        kept=len(kept),
        # Every arrival the sequence did not have to number came out of the pool.
        reused=arrived - drawn,
        released=len(set(previous.assigned) - kept),
        drawn=drawn,
        sequence_before=before, sequence_after=after,
        carried_from=carried_from, sequence_from=sequence_from,
    )


def write_venue_manifest(as_of: str, mic: str, tokens: VenueTokens,
                         started_at: str = "",
                         run: Optional[RunStats] = None) -> Path:
    """Write one venue's header, and the allocation table it points at.

    The table goes first and the header last, so the header's presence keeps
    meaning exactly what venues_with_manifest treats it as: this venue is done.
    A crash between the two leaves a table nothing references, which the next
    run overwrites. The other order would advertise a completed venue whose
    tokens were not on disk.

    Both are staged under a PID-scoped name and replaced on success, for the
    reason every writer here does it: two runs must not share a temp path, and a
    half-written manifest read as tomorrow's carry-forward would silently
    re-issue live tokens.
    """
    mic = mic.upper()
    path = _venue_manifest_path(as_of, mic)
    path.parent.mkdir(parents=True, exist_ok=True)

    alloc = write_alloc(_alloc_path(as_of, mic), tokens)
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
        "code": {"build_sha": build_sha(), "manifest_version": MANIFEST_VERSION},
        "allocation": {
            "venue_id": tokens.venue_id,
            "highest": tokens.highest,
            "count": len(tokens.assigned),
            "free_count": len(set(tokens.free)),
            # The table, and enough to prove it is the one this header describes.
            "path": alloc.name,
            "rows": len(tokens.assigned) + len(set(tokens.free)),
            "bytes": alloc.stat().st_size,
            "sha256": sha256_of(alloc),
        },
        "tokens": (run or RunStats()).as_dict(),
    }
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
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
