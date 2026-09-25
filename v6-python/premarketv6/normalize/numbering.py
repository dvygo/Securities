"""How a venue-day gets its counterTokenV2: the two allocators, as pure functions.

Nothing here reads or writes a file. The state store (state.py) supplies what a
run starts from and persists what it produced; this module only decides. Keeping
the decision pure is what lets it be tested across thousands of generated
histories rather than a handful of hand-built ones.

Two clocks drive it. VALID time is the trading day a run describes; TRANSACTION
time is when the run happens. The counter follows transaction time -- every run
draws above everything any run has ever issued. Which tokens a day starts from
follows valid time:

  live  A run on the newest trading day continues from the venue's latest
        state, the "golden record" every run leaves behind (allocate_live).
  fill  A run on an older trading day -- history before the first day, or a day
        missed in the middle -- is filled from the numbered days either side of
        it, and never takes anything back from the state (allocate_fill).

The contract both keep, and that tests/test_numbering.py generates histories to
break:

  1. Within a trade date no two scripts share a token, across every run of it.
  2. A script on two consecutive numbered days keeps its token, except where a
     fill recorded the break as an exception for exactly that pair.
  3. The counter never hands out the same number twice.
  4. Re-running a day with the same inputs changes nothing and draws nothing.
  5. A fill never changes or frees a token the latest state holds.
"""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence as Seq, Set, Tuple

from .counter_token import Sequence, VenueTokens, carry_forward

LIVE = "live"
FILL = "fill"


@dataclass
class Holdings:
    """A venue's latest state: every token it holds, and the ones it may hand out.

    `last_date` is the most recent trading day each holding was handed out on.
    It is what makes numbering append-only within a date: a holding handed out
    today is never released today, even when a later pass of the same day -- a
    re-run, or another vendor for the same market -- does not carry the script.
    Released tokens go back to `free` only once the trading day has moved on.
    """
    venue_id: int
    assigned: Dict[str, int] = field(default_factory=dict)
    free: List[int] = field(default_factory=list)
    last_date: Dict[str, str] = field(default_factory=dict)

    def copy(self) -> "Holdings":
        return Holdings(self.venue_id, dict(self.assigned), sorted(set(self.free)),
                        dict(self.last_date))

    @property
    def highest(self) -> int:
        return max(list(self.assigned.values()) + list(self.free), default=0)


@dataclass
class DayAlloc:
    """One venue-day as its manifest records it.

    `assigned` is what this run's output carries. `retained` was handed out on
    the same date by an earlier run and is absent from this one; it can never be
    handed to another script on that date. `free` is the pool as the day left it.
    """
    date: str
    assigned: Dict[str, int] = field(default_factory=dict)
    retained: Dict[str, int] = field(default_factory=dict)
    free: List[int] = field(default_factory=list)

    def token_of(self, script: str) -> Optional[int]:
        token = self.assigned.get(script)
        return token if token is not None else self.retained.get(script)

    def holders(self) -> Dict[int, str]:
        """token -> script, for every script this day handed a token to."""
        out = {t: s for s, t in self.retained.items()}
        out.update({t: s for s, t in self.assigned.items()})
        return out


@dataclass(frozen=True)
class TokenException:
    """A script whose token differs from a neighbouring day's, recorded on purpose.

    `wanted_from` is the neighbour whose token continuity asked for. The record
    applies to that pair only: when a later fill lands between the two days the
    break moves with it, and this record is superseded rather than rewritten, so
    a written file never changes.
    """
    script: str
    token: int
    wanted: int
    wanted_from: str
    lost_to: str

    def as_row(self) -> dict:
        return {"script": self.script, "token": self.token, "wanted": self.wanted,
                "wanted_from": self.wanted_from, "lost_to": self.lost_to}


@dataclass
class Plan:
    """What a run decided. The session persists it; nothing here has been written."""
    mode: str
    date: str
    day: DayAlloc
    state_after: Holdings
    exceptions: List[TokenException] = field(default_factory=list)
    retired: List[int] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    neighbours: Dict[str, str] = field(default_factory=dict)
    counter_before: int = 0
    counter_after: int = 0

    def token(self, script: str) -> Optional[int]:
        return self.day.assigned.get(script)


def _present(scripts: Iterable) -> List[str]:
    return sorted(set(s for s in scripts if s))


def allocate_live(date: str, scripts: Iterable, state: Optional[Holdings],
                  previous: Optional[DayAlloc], sequence: Sequence,
                  venue_id: int) -> Plan:
    """The newest trading day, continued from the venue's latest state.

    This is carry_forward against the state rather than against a day: kept
    scripts keep the state's token, departures release, arrivals reclaim their
    own previous token when it is in the pool, then drain the pool, then draw.
    Two things differ from a plain day-to-day carry:

      - Append-only within the date. A holding already handed out on `date` is
        never released on `date`; it is recorded as `retained` instead.
      - A script on `previous` (the previous numbered day) that ends up on a
        different token -- it could not reclaim its own -- is recorded as an
        exception, so the break is on record rather than silent.
    """
    before = sequence.issued
    present = _present(scripts)
    if state is None:
        start = VenueTokens(venue_id)
        releasable = start
        retained: Dict[str, int] = {}
    else:
        retained = {s: t for s, t in state.assigned.items()
                    if s not in present and state.last_date.get(s) == date}
        releasable = VenueTokens(
            venue_id,
            {s: t for s, t in state.assigned.items() if s not in retained},
            list(state.free))
    prefer = dict(previous.assigned) if previous is not None else None
    if previous is not None:
        prefer.update({s: t for s, t in previous.retained.items() if s not in prefer})
    tokens = carry_forward(releasable if state is not None else None, present,
                           venue_id, sequence, prefer=prefer)

    day = DayAlloc(date, dict(tokens.assigned), dict(retained),
                   sorted(set(tokens.free) - set(retained.values())))
    last_date = {s: date for s in tokens.assigned}
    last_date.update({s: (state.last_date.get(s) or date) for s in retained})
    after = Holdings(venue_id, {**tokens.assigned, **retained},
                     sorted(set(tokens.free) - set(retained.values())), last_date)

    exceptions = breaks(day.assigned, retained, (previous,))

    kept = 0 if state is None else sum(1 for s in present if s in state.assigned)
    counts = {"scripts": len(present), "kept": kept, "arrived": len(present) - kept,
              "retained": len(retained), "drawn": sequence.issued - before,
              "released": 0 if state is None else sum(
                  1 for s in state.assigned if s not in present and s not in retained),
              "exceptions": len(exceptions)}
    return Plan(LIVE, date, day, after, exceptions, [], counts,
                {"earlier": previous.date if previous is not None else ""},
                before, sequence.issued)


def allocate_fill(date: str, scripts: Iterable, own: Optional[DayAlloc],
                  earlier: Optional[DayAlloc], later: Optional[DayAlloc],
                  state: Optional[Holdings], sequence: Sequence,
                  venue_id: int) -> Plan:
    """An older trading day, filled from the numbered days either side of it.

    `earlier`/`later` are the nearest numbered days before and after `date`
    (either may be None). `own` is the day's own allocation when it was filled
    before -- a re-run. Tokens are decided in a fixed order, tracking `used`, the
    tokens already handed out on this date:

      0. The day's own scripts keep their own tokens (a re-run is idempotent).
      1. A script on the later day takes the later day's token -- the later day
         wins, because that is the chain that leads to today. If the earlier day
         holds it on a different token, the break is recorded.
      2. A script only on the earlier day takes the earlier day's token.
      3. A neighbour token already in `used` is a clash: recorded, and the
         script joins everyone else in step 4.
      4. Everyone else, sorted: their own state token when it is free to use
         here, else the lowest state.free, else a fresh number. Tokens either
         neighbour holds for OTHER scripts are never handed out here -- that
         would set up a clash the day a fill lands between them.

    Nothing is released, and no existing holding changes. Scripts that got a
    number in step 4 and held nothing before become holdings; a script that
    already held a state token but could not use it here takes a day-local
    number that is retired -- held by nobody afterwards, never re-issued.
    """
    before = sequence.issued
    present = _present(scripts)
    base = state.copy() if state is not None else Holdings(venue_id)
    day: Dict[str, int] = {}
    used: Set[int] = set()
    counts = {"own": 0, "from_later": 0, "from_earlier": 0, "from_state": 0,
              "from_free": 0, "fresh": 0, "retired": 0}

    def give(script: str, token: int, source: str) -> None:
        day[script] = token
        used.add(token)
        counts[source] += 1

    retained: Dict[str, int] = {}
    if own is not None:                                      # step 0
        mine = {**own.retained, **own.assigned}
        for s in present:
            if s in mine:
                give(s, mine[s], "own")
        retained = {s: t for s, t in mine.items() if s not in day}
        used.update(retained.values())

    # Steps 1-3. The later day is taken in full before the earlier one, which is
    # what "the later day wins" means: when an earlier-only script wants a token
    # the later day already gave out here, it finds it in `used` and falls through
    # to step 4. A script on both whose later token is taken (only a re-run can do
    # that) still gets its earlier token if that one is free, keeping one side of
    # the pair stable rather than neither.
    for neighbour, source in ((later, "from_later"), (earlier, "from_earlier")):
        if neighbour is None:
            continue
        for s in present:
            if s in day or s not in neighbour.assigned:
                continue
            token = neighbour.assigned[s]
            if token not in used:
                give(s, token, source)
    rest = [s for s in present if s not in day]

    # Every token either neighbour hands to anyone. A step-4 number must avoid
    # them: handing one to a different script here is what later becomes a clash
    # when a fill lands between this day and that neighbour.
    blocked: Dict[int, str] = {}
    for neighbour in (earlier, later):
        if neighbour is not None:
            blocked.update(neighbour.holders())

    def usable(script: str, token: int) -> bool:
        return token not in used and blocked.get(token, script) == script

    candidates = [t for t in sorted(set(base.free)) if t not in blocked]
    cursor = 0
    taken_from_pool: Set[int] = set()
    retired: List[int] = []
    for s in sorted(rest):                                   # step 4
        held = base.assigned.get(s)
        if held is not None and usable(s, held):
            give(s, held, "from_state")
            continue
        while cursor < len(candidates) and candidates[cursor] in used:
            cursor += 1
        if cursor < len(candidates):
            token = candidates[cursor]
            cursor += 1
            taken_from_pool.add(token)
            give(s, token, "from_free")
        else:
            give(s, sequence.take(), "fresh")
        if held is None:
            base.assigned[s] = day[s]
            base.last_date[s] = date
        else:
            retired.append(day[s])
            counts["retired"] += 1
    base.free = sorted(set(base.free) - taken_from_pool)

    exceptions = breaks(day, retained, (earlier, later))
    counts.update({"scripts": len(present), "exceptions": len(exceptions),
                   "retained": len(retained), "drawn": sequence.issued - before})
    held_now = set(day.values()) | set(retained.values())
    day_alloc = DayAlloc(date, day, retained, sorted(set(base.free) - held_now))
    return Plan(FILL, date, day_alloc, base, exceptions, sorted(retired), counts,
                {"earlier": earlier.date if earlier is not None else "",
                 "later": later.date if later is not None else ""},
                before, sequence.issued)


def breaks(day: Dict[str, int], retained: Dict[str, int],
           neighbours: Seq[Optional[DayAlloc]]) -> List[TokenException]:
    """Every script whose token differs from a neighbour's, one record per pair.

    Computed from the finished day rather than noted while allocating, so it
    cannot miss one: a clash, the neighbours disagreeing, a re-run's own token --
    whatever caused a break, it is here. Only the neighbour's `assigned` counts,
    because that is what its file carries and what check-tokens compares.
    """
    holders = {t: s for s, t in retained.items()}
    holders.update({t: s for s, t in day.items()})
    present = [n for n in neighbours if n is not None]
    out: List[TokenException] = []
    for s in sorted(day):
        for neighbour in present:
            wanted = neighbour.assigned.get(s)
            if wanted is None or wanted == day[s]:
                continue
            others = [n.date for n in present if n is not neighbour]
            lost_to = holders.get(wanted) or (f"chain:{others[0]}" if others else "pool")
            out.append(TokenException(s, day[s], wanted, neighbour.date, lost_to))
    return out


def neighbours(date: str, numbered: Seq[str]) -> Tuple[Optional[str], Optional[str]]:
    """The nearest numbered days strictly before and after `date`."""
    earlier = max((d for d in numbered if d < date), default=None)
    later = min((d for d in numbered if d > date), default=None)
    return earlier, later
