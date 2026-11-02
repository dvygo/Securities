"""check-state: the numbering state store, audited.

check-tokens validates the days. This validates what every run starts from --
data/_state/ -- against the days on disk and the run log, so a state that has
drifted from the data is caught before a run numbers against it:

  head readable     the head parses and is a version this build reads
  snapshot          each venue's snapshot exists and hashes to what the head says
  newest on disk    no day newer than the head knows about -- one would mean a day
                    was numbered outside a numbering session
  venues disjoint   no token held by two venues in the state
  counter covers    the head's counter is at or above every number on disk
  commit finished   no staged file left over, and the last commit's header written
  runs closed       every START in the run log has a COMPLETE or FAIL
"""
from typing import List

from ..normalize import counter_token, state
from .report import ALL, V2, Check, report


def collect() -> List[Check]:
    checks: List[Check] = []
    try:
        head = state.read_head()
    except state.StateCorrupt as exc:
        return [Check("state", "*", "head readable", False, str(exc))]
    if head is None:
        return [Check("state", "*", "head readable", False,
                      f"no head at {state.head_path()} -- run init-state")]
    checks.append(Check("state", "*", "head readable", True,
                        f"counter {head.counter:,}, {len(head.venues)} venue(s), "
                        f"last run {head.last_run.get('run_id', '-')}"))

    owners = {}
    shared = 0
    highest_held = 0
    for mic, venue in sorted(head.venues.items()):
        try:
            holdings = state.load_snapshot(mic, venue)
        except state.StateCorrupt as exc:
            checks.append(Check("state", mic, "snapshot", False, str(exc), tag=V2))
            continue
        checks.append(Check("state", mic, "snapshot", True,
                            f"{venue.snapshot}: {len(holdings.assigned):,} held, "
                            f"{len(holdings.free):,} free", tag=V2))
        for token in list(holdings.assigned.values()) + holdings.free:
            if owners.setdefault(token, mic) != mic:
                shared += 1
        highest_held = max(highest_held, holdings.highest)
        days = state.venue_days(mic)
        newest = days[-1] if days else ""
        checks.append(Check(
            "state", mic, "newest on disk", newest <= venue.newest,
            f"state newest {venue.newest or '-'} ({venue.newest_mode or '-'}), "
            f"newest on disk {newest or '-'}"
            + ("" if newest <= venue.newest else
               " -- a day was numbered outside a numbering session")))

    checks.append(Check("state", "*", "venues disjoint", shared == 0,
                        f"{shared:,} token(s) held by more than one venue", tag=V2))

    on_disk, where = counter_token.highest_issued()
    top = max(on_disk, highest_held)
    checks.append(Check(
        "state", "*", "counter covers", head.counter >= top,
        f"head counter {head.counter:,}; highest on disk {on_disk:,} ({where or '-'}), "
        f"highest held {highest_held:,}", tag=V2))

    root = state.paths.data_root()
    staged = sorted(str(p.relative_to(root)) for p in root.rglob(f"*{state.STAGED}"))
    commit = head.last_commit or {}
    header_ok = True
    if commit.get("header"):
        on_disk_header = counter_token._read_json(
            counter_token.venue_manifest_path(commit["date"], commit["mic"]))
        header_ok = (on_disk_header.get("numbering") or {}).get("run_id") == commit["run_id"]
    checks.append(Check(
        "state", "*", "commit finished", not staged and header_ok,
        ("no staged files; " if not staged else f"{len(staged)} staged file(s), e.g. "
                                                 f"{staged[0]}; ")
        + ("last commit's header written" if header_ok else
           f"last commit {commit.get('run_id')} has no header -- the next run's "
           f"recovery writes it")))

    events = state.read_events()
    ended = {e["run"]["runId"] for e in events if e["eventType"] in ("COMPLETE", "FAIL")}
    open_runs = [e["run"]["runId"] for e in events
                 if e["eventType"] == "START" and e["run"]["runId"] not in ended]
    checks.append(Check(
        "state", "*", "runs closed", not open_runs,
        f"{len(events):,} event(s) in the run log"
        + (f"; {len(open_runs)} run(s) never closed, e.g. {open_runs[0]} -- running "
           f"now, or interrupted (the next run's recovery closes it)" if open_runs else ""),
        hard=False))
    for check in checks:
        if check.tag != V2:
            check.tag = ALL
    return checks


def run() -> int:
    return report(collect(), suite="check-state")
