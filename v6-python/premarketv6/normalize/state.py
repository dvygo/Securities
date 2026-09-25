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
import tarfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

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


STAGED = ".staged"


def write_snapshot(mic: str, run_id: str, holdings: Holdings,
                   current: Optional[VenueHead] = None,
                   staged: bool = False) -> Tuple[str, str, int]:
    """Write a venue's state as a new snapshot; returns (relative path, sha, rows).

    A snapshot is never overwritten. When the content is byte-identical to the
    venue's current snapshot -- a re-run that changed nothing -- the current one
    is returned instead of writing a duplicate.

    `staged` writes it under `<name>.staged`: the numbering session commits the
    head first and promotes the file after, so a crash before the commit leaves
    only a staged file that recovery deletes. The returned path is always the
    final one, which is what the head records.
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
    os.replace(staging, path.with_name(path.name + STAGED) if staged else path)
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


# -- identity ----------------------------------------------------------------------

_run_counter = count(1)


def new_run_id(date: str, mode: str) -> str:
    """Sortable by transaction time, unique even for runs inside one second."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{now}-{date}-{mode}-{os.getpid()}-{next(_run_counter)}"


def operator() -> str:
    """Who is running this: user@host, recorded with every run."""
    import getpass
    import socket
    try:
        user = getpass.getuser()
    except Exception:                                # noqa: BLE001 - no tty, no passwd entry
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


# -- the lock ------------------------------------------------------------------------

class LockHeld(RuntimeError):
    """Another numbering run is in progress. Runs are serialised; this one refuses."""


@contextmanager
def lock(command: str) -> Iterator[None]:
    """Hold data/_state/.lock for the length of one numbering command.

    Non-blocking on purpose: a second run refuses at once, naming the holder,
    rather than waiting and piling up behind it. Two runs drawing from the same
    counter at the same time is the one failure no amount of care afterwards can
    repair.
    """
    import fcntl
    path = state_dir() / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip() or "unknown holder"
        handle.close()
        raise LockHeld(f"another numbering run holds {path}: {holder}. Runs are "
                       f"serialised so two can never hand out the same number; "
                       f"wait for it to finish.") from None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "command": command,
                                 "operator": operator(), "since": counter_token.utc_now()}))
        handle.flush()
        yield
    finally:
        try:
            handle.seek(0)
            handle.truncate()
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# -- the run log -------------------------------------------------------------------

RUNS_NAME = "runs.jsonl"


def runs_path() -> Path:
    return state_dir() / RUNS_NAME


def append_event(event: dict) -> None:
    """One line per event, fsynced: the run log is the audit trail, not a cache."""
    path = runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_events() -> List[dict]:
    path = runs_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def event(event_type: str, run_id: str, job: str, facets: dict, inputs=(), outputs=(),
          parent: str = "") -> dict:
    """An OpenLineage-shaped run event.

    The field names follow the OpenLineage spec (eventType, eventTime, run.runId,
    job, inputs/outputs, facets) so the log can be fed to a lineage catalog
    later without rewriting it. Our own detail lives in one custom facet.
    """
    run = {"runId": run_id, "facets": {"premarketv6": facets}}
    if parent:
        run["facets"]["parent"] = {"run": {"runId": parent}}
    return {
        "eventType": event_type,
        "eventTime": counter_token.utc_now(),
        "producer": f"premarketv6/{counter_token.build_sha()}",
        "job": {"namespace": "premarketv6", "name": job},
        "run": run,
        "inputs": [{"namespace": "file", "name": a["path"],
                    "facets": {"digest": {"sha256": a.get("sha256", "")},
                               "rows": a.get("rows", 0)}} for a in inputs],
        "outputs": [{"namespace": "file", "name": a["path"],
                     "facets": {"digest": {"sha256": a.get("sha256", "")},
                                "rows": a.get("rows", 0)}} for a in outputs],
    }


# -- backups -------------------------------------------------------------------------

BACKUPS_KEEP = 10


def backups_dir() -> Path:
    return state_dir() / "backups"


def backup(run_id: str) -> Tuple[Path, List[str]]:
    """Every day's manifests and the state head, as one tar.gz; returns (path, pruned).

    Taken before a --dates run and before init-state. Restoring is one command
    from the data root: `tar -xzf _state/backups/<run_id>.tar.gz`. Only the newest
    BACKUPS_KEEP are kept; the names pruned are returned so the run log records
    them.
    """
    root = paths.data_root()
    target = backups_dir() / f"{run_id}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    with tarfile.open(staging, "w:gz") as archive:
        for day in counter_token.numbered_day_dirs():
            directory = counter_token.manifests_dir(day)
            archive.add(directory, arcname=str(directory.relative_to(root)))
        if head_path().exists():
            archive.add(head_path(), arcname=str(head_path().relative_to(root)))
    os.replace(staging, target)
    existing = sorted(p for p in backups_dir().glob("*.tar.gz"))
    pruned = []
    for old in existing[:-BACKUPS_KEEP]:
        old.unlink()
        pruned.append(old.name)
    return target, pruned


# -- recovery --------------------------------------------------------------------------

def recover(head: Head, log: Callable[[str], None] = print) -> List[str]:
    """Finish or clean up a numbering run that was interrupted.

    A run commits by replacing the head, which records the commit in
    `last_commit`: the staged files it wrote and the header it meant to write.

      - A staged file the head's last commit names is promoted (after its digest
        is checked). One it does not name belongs to a run that never committed
        and is deleted -- its numbers were reserved, so they are leaked, never
        reissued.
      - If the committed day's header is missing or belongs to an older run, it
        is written from the head.
      - A START in the run log with no COMPLETE or FAIL is closed as FAIL.
    """
    actions: List[str] = []
    commit = head.last_commit or {}
    wanted = {item["staged"]: item for item in commit.get("staged", [])}
    root = paths.data_root()
    for staged in sorted(list(root.glob(f"*/{paths.TRANSFORM_DIR}/manifests/*{STAGED}"))
                         + list(state_dir().glob(f"*/*{STAGED}"))):
        relative = str(staged.relative_to(root))
        item = wanted.get(relative)
        if item is None:
            staged.unlink()
            actions.append(f"deleted uncommitted {relative}")
            continue
        if counter_token.sha256_of(staged) != item["sha256"]:
            raise StateCorrupt(f"recovery: {relative} does not match the digest the head "
                               f"committed; refusing to promote it. Restore from "
                               f"{backups_hint()}")
        os.replace(staged, root / item["final"])
        actions.append(f"promoted {item['final']}")
    if commit.get("header"):
        date, mic = commit["date"], commit["mic"]
        on_disk = counter_token._read_json(counter_token.venue_manifest_path(date, mic))
        if (on_disk.get("numbering") or {}).get("run_id") != commit["run_id"]:
            counter_token.write_header(date, mic, commit["header"])
            actions.append(f"wrote the {mic} {date} header for {commit['run_id']}")
    events = read_events()
    ended = {e["run"]["runId"] for e in events if e["eventType"] in ("COMPLETE", "FAIL")}
    for e in events:
        if e["eventType"] == "START" and e["run"]["runId"] not in ended:
            append_event(event("FAIL", e["run"]["runId"], e["job"]["name"],
                               {"recovered": True, "error": "interrupted before it finished"}))
            ended.add(e["run"]["runId"])
            actions.append(f"closed interrupted run {e['run']['runId']} as FAIL")
    for line in actions:
        log(f"  recovery: {line}")
    return actions


# -- init ------------------------------------------------------------------------------

def init_state(reason: str, dry_run: bool = False,
               log: Callable[[str], None] = print) -> Optional[Head]:
    """Build the first head from what is on disk: each venue's newest manifest.

    Refuses when a head exists (re-initialising would discard what every run
    since recorded), and refuses unless the venues' holdings are disjoint and
    the counter sits above every held token -- a day numbered by the old code,
    from 1, would otherwise become every future day's collision.
    """
    from .. import config
    if read_head() is not None:
        raise StateCorrupt(f"a state head already exists at {head_path()}; init-state "
                           f"runs once per host")
    holdings: Dict[str, Tuple[Holdings, str, str, dict]] = {}
    for cfg in sorted(config.load_exchanges().values(), key=lambda c: c.venue_name):
        mic = cfg.venue_name.upper()
        days = venue_days(mic)
        if not cfg.venue_id or not days:
            continue
        newest = days[-1]
        entry = counter_token.venue_entry(newest, mic)
        header = counter_token._read_json(counter_token.venue_manifest_path(newest, mic))
        mode = (header.get("numbering") or {}).get("mode", "live")
        assigned = {**entry.get("retained", {}), **entry["assigned"]}
        venue = Holdings(int(entry["venue_id"]), assigned, sorted(entry["free"]),
                         {s: newest for s in assigned})
        holdings[mic] = (venue, newest, mode, header)

    owners: Dict[int, str] = {}
    clashes: List[str] = []
    for mic, (venue, _, _, _) in holdings.items():
        for token in list(venue.assigned.values()) + venue.free:
            other = owners.setdefault(token, mic)
            if other != mic:
                clashes.append(f"{token} ({other} and {mic})")
    if clashes:
        raise StateCorrupt(f"venues share {len(clashes):,} token(s), e.g. "
                           f"{', '.join(clashes[:5])}. A day was numbered in isolation "
                           f"(the old code numbered an older date from 1). Fix those "
                           f"days before initialising the state.")
    counter, source = global_counter(None)
    top = max((v.highest for v, _, _, _ in holdings.values()), default=0)
    if top > counter:
        raise StateCorrupt(f"a venue holds token {top:,} but the highest number on "
                           f"record is {counter:,}; the counter would reissue it")

    for mic, (venue, newest, mode, _) in holdings.items():
        log(f"  {mic}: newest {newest} ({mode}), {len(venue.assigned):,} held, "
            f"{len(venue.free):,} free, highest {venue.highest:,}")
    log(f"  counter: {counter:,} (from {source or 'nothing'})")
    if dry_run:
        log("  DRY RUN: nothing written")
        return None

    run_id = new_run_id(max((n for _, n, _, _ in holdings.values()), default="00000000"),
                        "init")
    append_event(event("START", run_id, "init-state",
                       {"reason": reason, "operator": operator()}))
    archive, pruned = backup(run_id)
    head = Head(counter=counter)
    inputs = []
    for mic, (venue, newest, mode, header) in holdings.items():
        relative, digest, rows = write_snapshot(mic, run_id, venue)
        head.venues[mic] = VenueHead(venue.venue_id, relative, digest, rows,
                                     newest=newest, newest_mode=mode,
                                     live_date=newest if mode == "live" else "",
                                     run_id=run_id, updated_at=counter_token.utc_now())
        allocation = header.get("allocation") or {}
        inputs.append({"path": f"{newest}/{paths.TRANSFORM_DIR}/manifests/"
                               f"{allocation.get('path', mic)}",
                       "sha256": allocation.get("sha256", ""),
                       "rows": allocation.get("rows", 0)})
    head.last_run = {"run_id": run_id, "command": "init-state", "reason": reason,
                     "operator": operator(), "completed_at": counter_token.utc_now()}
    write_head(head)
    append_event(event("COMPLETE", run_id, "init-state",
                       {"reason": reason, "operator": operator(), "counter": counter,
                        "venues": sorted(holdings), "backup": archive.name,
                        "backups_pruned": pruned}, inputs=inputs))
    log(f"  state initialised: {head_path()} (backup {archive.name})")
    return head

