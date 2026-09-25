"""The state store: what every numbering run starts from and leaves behind.

    data/_state/
      manifest.json                  the head: the counter, what ran last, and for
                                     each venue the snapshot that is its state now
      <MIC>/<run_id>.alloc.parquet   state snapshots -- written once, never changed
      runs.jsonl                     the append-only run log
      backups/<run_id>.tar.gz        taken before a --dates run and before init-state
      .lock                          one numbering run at a time

Per-day manifests stay the record of what each day was given. The state is the
record of what each venue holds NOW -- the golden record a live run continues
from, whichever run (live, or a fill of an older day) touched it last.

The head is replaced atomically, and replacing it is the commit point of every
numbering run: a snapshot nothing points at is only a file, and a day's header
is written after the head records the run. That ordering is what lets a crash
at any point be recovered without guessing.
"""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

from .. import paths
from . import counter_token
from .numbering import DayAlloc, Holdings

STATE_DIR = "_state"
HEAD_NAME = "manifest.json"
HEAD_VERSION = 1

# Snapshot row states. `script` is null exactly when the token is free.
ASSIGNED = counter_token.ALLOC_ASSIGNED
FREE = counter_token.ALLOC_FREE


class StateMissing(RuntimeError):
    """No head yet. Numbering refuses to guess one -- run `init-state`."""


class StateCorrupt(ValueError):
    """The head or a snapshot it names does not verify. Refuse; never renumber."""


def state_dir() -> Path:
    return paths.data_root() / STATE_DIR


def head_path() -> Path:
    return state_dir() / HEAD_NAME


def snapshot_path(relative: str) -> Path:
    return state_dir() / relative


# -- the head ------------------------------------------------------------------

@dataclass
class VenueHead:
    """Where one venue stands: its current snapshot and its newest numbered day."""
    venue_id: int
    snapshot: str                  # path relative to data/_state/
    sha256: str
    rows: int
    newest: str = ""               # newest numbered day, any mode
    newest_mode: str = ""          # "live" or "fill"
    live_date: str = ""            # newest day numbered live; "" when never live
    run_id: str = ""
    updated_at: str = ""


@dataclass
class Head:
    """The head document. `counter` is the highest number any run has issued."""
    counter: int = 0
    updated_at: str = ""
    last_run: Dict = field(default_factory=dict)
    venues: Dict[str, VenueHead] = field(default_factory=dict)
    last_commit: Optional[Dict] = None
    version: int = HEAD_VERSION

    def as_dict(self) -> dict:
        doc = asdict(self)
        doc["venues"] = {mic: asdict(vh) for mic, vh in sorted(self.venues.items())}
        return doc

    @classmethod
    def from_dict(cls, doc: dict) -> "Head":
        venues = {mic: VenueHead(**vh) for mic, vh in (doc.get("venues") or {}).items()}
        return cls(counter=int(doc.get("counter", 0)), updated_at=doc.get("updated_at", ""),
                   last_run=doc.get("last_run") or {}, venues=venues,
                   last_commit=doc.get("last_commit"),
                   version=int(doc.get("version", HEAD_VERSION)))


def read_head() -> Optional[Head]:
    """The head, or None when the state has never been initialised."""
    path = head_path()
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        head = Head.from_dict(doc)
    except (OSError, ValueError, TypeError) as exc:
        raise StateCorrupt(f"{path} is unreadable ({exc}). Refusing to number anything "
                           f"against a state that cannot be read; restore it from "
                           f"{backups_hint()}") from exc
    if head.version != HEAD_VERSION:
        raise StateCorrupt(f"{path} is version {head.version}; this build reads "
                           f"version {HEAD_VERSION}")
    return head


def require_head() -> Head:
    head = read_head()
    if head is None:
        raise StateMissing(
            f"no state head at {head_path()}. Numbering continues from the latest "
            f"state, so it has to exist first: run `python -m premarketv6 init-state "
            f"--reason \"...\"` once on this host.")
    return head


def write_head(head: Head) -> Path:
    """Replace the head atomically. This is the commit point of a numbering run."""
    path = head_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    head.updated_at = counter_token.utc_now()
    _atomic_write(path, json.dumps(head.as_dict(), indent=1, sort_keys=True).encode())
    return path


def backups_hint() -> str:
    return str(state_dir() / "backups")


def _atomic_write(path: Path, payload: bytes) -> None:
    """Stage under a PID-scoped name, fsync, rename, fsync the directory.

    The directory fsync is what makes the rename itself durable: without it a
    power cut can leave the old head on disk after a run that believed it had
    committed.
    """
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(staging, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# -- snapshots -----------------------------------------------------------------

def _snapshot_schema():
    return pa.schema([
        ("script", pa.string()),
        ("token", pa.int32()),
        ("state", pa.string()),
        ("last_date", pa.string()),
    ])


def _snapshot_table(holdings: Holdings) -> pa.Table:
    """Canonical order -- assigned by script, then free ascending -- so the same
    holdings always produce the same bytes and the same digest."""
    scripts = sorted(holdings.assigned)
    free = sorted(set(holdings.free))
    return pa.Table.from_arrays([
        pa.array(scripts + [None] * len(free), pa.string()),
        pa.array([holdings.assigned[s] for s in scripts] + free, pa.int32()),
        pa.array([ASSIGNED] * len(scripts) + [FREE] * len(free), pa.string()),
        pa.array([holdings.last_date.get(s, "") for s in scripts] + [None] * len(free),
                 pa.string()),
    ], schema=_snapshot_schema())


def write_snapshot(mic: str, run_id: str, holdings: Holdings,
                   current: Optional[VenueHead] = None) -> Tuple[str, str, int]:
    """Write a venue's state as a new snapshot; returns (relative path, sha, rows).

    A snapshot is never overwritten. When the content is byte-identical to the
    venue's current snapshot -- a re-run that changed nothing -- the current one
    is returned instead of writing a duplicate.
    """
    mic = mic.upper()
    relative = f"{mic}/{run_id}{counter_token.ALLOC_SUFFIX}"
    path = snapshot_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = _snapshot_table(holdings)
    staging = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    pq.write_table(table, staging, compression="zstd")
    digest = counter_token.sha256_of(staging)
    if current is not None and current.sha256 == digest \
            and snapshot_path(current.snapshot).exists():
        staging.unlink()
        return current.snapshot, current.sha256, current.rows
    if path.exists():
        staging.unlink()
        raise StateCorrupt(f"snapshot {path} already exists; snapshots are never "
                           f"overwritten (run ids must be unique)")
    os.replace(staging, path)
    return relative, digest, table.num_rows


def load_snapshot(mic: str, venue: VenueHead) -> Holdings:
    """A venue's state from its snapshot, verified against the digest the head holds."""
    path = snapshot_path(venue.snapshot)
    if not path.exists():
        raise StateCorrupt(f"{mic}: the head names snapshot {venue.snapshot}, which "
                           f"does not exist. Refusing to number against a state "
                           f"that cannot be read; restore from {backups_hint()}")
    actual = counter_token.sha256_of(path)
    if actual != venue.sha256:
        raise StateCorrupt(f"{mic}: snapshot {venue.snapshot} hashes to {actual}, the "
                           f"head records {venue.sha256}. It changed after it was "
                           f"written.")
    table = pq.read_table(path)
    holdings = Holdings(venue.venue_id)
    for script, token, state, last_date in zip(*(table.column(c).to_pylist() for c in
                                                 ("script", "token", "state", "last_date"))):
        if state == ASSIGNED and script:
            holdings.assigned[script] = int(token)
            holdings.last_date[script] = last_date or ""
        elif state == FREE and not script:
            holdings.free.append(int(token))
        else:
            raise StateCorrupt(f"{mic}: snapshot {venue.snapshot} has a row that is "
                               f"neither assigned-with-script nor free-without-one")
    holdings.free.sort()
    return holdings


# -- what is on disk ---------------------------------------------------------------

def venue_days(mic: str) -> List[str]:
    """Every day that holds a manifest header for `mic`, oldest first."""
    mic = mic.upper()
    return [d for d in counter_token.numbered_day_dirs()
            if (counter_token.manifests_dir(d) / f"{mic}.json").exists()]


def day_alloc(date: str, mic: str) -> Optional[DayAlloc]:
    """A day's allocation for one venue, as the numbering needs it, or None."""
    entry = counter_token.venue_entry(date, mic)
    if not entry:
        return None
    return DayAlloc(date, dict(entry.get("assigned") or {}),
                    dict(entry.get("retained") or {}),
                    sorted(int(t) for t in (entry.get("free") or [])))


def global_counter(head: Optional[Head]) -> Tuple[int, str]:
    """The highest number any run has issued: the head, or anything on disk above it.

    Either source alone is enough -- the head if every day directory were lost,
    the scan if the head were -- and a new number is drawn above both.
    """
    scanned, where = counter_token.highest_issued()
    recorded = head.counter if head is not None else 0
    if recorded >= scanned:
        return recorded, HEAD_NAME if recorded else ""
    return scanned, where

