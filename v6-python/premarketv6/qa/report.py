"""One verdict shape and one printer, shared by every check in this package.

Kept apart from the checks themselves so `tokens.py` and `lineage.py` render
identically and a caller can merge their results into a single report -- a run
that validates tokens and lineage should not print two different-looking tables.
"""
from dataclasses import dataclass
from typing import Sequence


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

    @property
    def status(self) -> str:
        return "PASS" if self.ok else ("FAIL" if self.hard else "WARN")


def report(checks: Sequence[Check]) -> int:
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
        print(f"  [{check.status}] {check.name:<18} {check.detail}")

    failed = [c for c in checks if not c.ok and c.hard]
    warned = [c for c in checks if not c.ok and not c.hard]
    print(f"\n{len(checks)} check(s): {len(checks) - len(failed) - len(warned)} pass, "
          f"{len(failed)} fail, {len(warned)} warn")
    for check in failed:
        print(f"  FAIL {check.day} {check.venue} {check.name}: {check.detail}")
    return 1 if failed else 0
