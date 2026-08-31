"""One verdict shape and one printer, shared by every check in this package.

Kept apart from the checks themselves so `tokens.py` and `lineage.py` render
identically and a caller can merge their results into a single report -- a run
that validates tokens and lineage should not print two different-looking tables.

Every check carries a tag saying which token column it speaks for, because the
two are not interchangeable and a report that blurs them is useless as evidence:

  v2   counterTokenV2 -- the column the plugin and the Postgres push carry
  ALL  neither in particular: file placement, row counts, schema

report() prints, and also writes one minimal .txt per tag under
paths.qat_dir(), so a claim about v2 can be quoted from v2.txt without the
reader having to filter a combined log by eye.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

V2, ALL = "v2", "ALL"
TAGS = (V2, ALL)


@dataclass
class Check:
    """One verdict.

    `hard` is False for a property the pipeline never promised. Those are worth
    a number -- counterTokenV2's offset reuse, a clamped download window -- but
    failing the run on them would train everyone to ignore the exit code.
    """
    day: str
    venue: str
    name: str
    ok: bool
    detail: str
    hard: bool = True
    tag: str = ALL

    @property
    def status(self) -> str:
        return "PASS" if self.ok else ("FAIL" if self.hard else "WARN")

    def line(self) -> str:
        return (f"[{self.tag}] {self.status:<4} {self.day:<21} {self.venue:<5} "
                f"{self.name:<18} {self.detail}")


def _tally(checks: Sequence[Check]) -> str:
    failed = sum(1 for c in checks if not c.ok and c.hard)
    warned = sum(1 for c in checks if not c.ok and not c.hard)
    return (f"{len(checks)} check(s): {len(checks) - failed - warned} pass, "
            f"{failed} fail, {warned} warn")


def write_reports(checks: Sequence[Check], suite: str,
                  directory: Optional[Path] = None) -> list:
    """One .txt per tag, plus ALL.txt holding every line.

    Overwritten rather than appended: the file describes the last run, and a
    file that grows without saying which run each line came from is not
    evidence of anything. The header carries the suite, the timestamp and the
    tally so a quoted file stands on its own.
    """
    from .. import paths

    directory = Path(directory) if directory else paths.qat_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    written = []
    for tag in TAGS:
        subset = list(checks) if tag == ALL else [c for c in checks if c.tag == tag]
        path = directory / f"{suite}.{tag}.txt"
        body = "\n".join(c.line() for c in subset)
        path.write_text(
            f"# {suite} [{tag}]  {stamp}\n"
            f"# {_tally(subset)}\n"
            + (body + "\n" if body else "# no checks carried this tag\n"),
            encoding="utf-8")
        written.append(path)
    return written


def report(checks: Sequence[Check], suite: Optional[str] = None) -> int:
    """Print grouped by day and venue. Non-zero when a hard check failed.

    Failures are repeated at the end: on a five-day, six-venue run the body is
    hundreds of lines and the one FAIL in the middle of it does not survive a
    scrollback.
    """
    scope = None
    for check in checks:
        if (check.day, check.venue) != scope:
            scope = (check.day, check.venue)
            print(f"\n{check.day}  {check.venue}")
        print(f"  [{check.tag}] [{check.status}] {check.name:<18} {check.detail}")

    failed = [c for c in checks if not c.ok and c.hard]
    print(f"\n{_tally(checks)}")
    for check in failed:
        print(f"  FAIL {check.day} {check.venue} {check.name}: {check.detail}")

    if suite and checks:
        for path in write_reports(checks, suite):
            print(f"  wrote {path}")
    return 1 if failed else 0
