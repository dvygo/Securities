"""The numbering session: one command's numbering, from the lock to the last commit.

Every normalizer used to repeat the same block -- open the anchor, open the
sequence, carry forward, write the sequence, write the manifest -- three copies
of the one piece of code that must never disagree with itself. Each now does:

    venue = opts.numbering.venue(date, mic, venue_id)   # guards: NumberingRefused
    plan  = venue.plan(scripts)                          # numbering.py decides
    venue.reserve(plan)                                  # before anything is published
    ...write the normalized parquet with plan.token(script)...
    venue.commit(plan, started_at, inputs, outputs)

A session holds the state lock for the whole command, runs recovery before it
numbers anything, and owns the one Sequence every venue and date in the command
draws from. commit() is the protocol that makes a crash recoverable at any step:

    1. reserve   the day's _sequence.json is raised to the counter -- before the
                 parquet is promoted, so a published token is always covered
    2. publish   the normalizer promotes the parquet
    3. stage     allocation table, exceptions table and state snapshot, under
                 `.staged` names nothing reads
    4. COMMIT    the head is replaced, naming the staged files and the header
    5. promote   the staged files take their final names
    6. header    the day's header -- "this venue is done" -- is written
    7. log       the run log records the venue-day

A preview session (`--dates --dry-run`) runs the same decisions against an
in-memory overlay -- later dates see the earlier ones it planned, and the
counter advances exactly as the real run would -- and writes nothing.
"""
import datetime as _dt
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .. import paths
from . import counter_token, numbering, state
from .numbering import FILL, LIVE, DayAlloc, Holdings, Plan


class NumberingRefused(ValueError):
    """This venue-day must not be numbered in this mode.

    A ValueError, so the `except ValueError` the normalizers already wrap their
    numbering in catches it and skips the venue with a CRITICAL line.
    """


@dataclass
class Outcome:
    date: str
    mic: str
    status: str          # done | refused | no-input | previewed
    detail: str = ""


def _within_window(earlier: str, later: str) -> bool:
    a = _dt.datetime.strptime(earlier, "%Y%m%d").date()
    b = _dt.datetime.strptime(later, "%Y%m%d").date()
    return (b - a).days <= counter_token.MANIFEST_LOOKBACK_DAYS


def of(opts) -> "Session":
    """The run's numbering session. Normalizing outside one is a programming error:
    counterTokenV2 is only ever assigned under the lock, against the state."""
    session = getattr(opts, "numbering", None)
    if session is None:
        raise RuntimeError("counterTokenV2 is only assigned inside a numbering session "
                           "(`premarketv6 normalize` / `plugin` open one); refusing to "
                           "number outside it")
    return session


class Session:
    """One numbering command. Use as a context manager around the whole run."""

    def __init__(self, command: str, mode: str, reason: str = "", preview: bool = False,
                 label: str = "", log: Callable[[str], None] = print):
        self.command, self.mode, self.reason = command, mode, reason
        self.preview = preview
        self.label = label or mode
        self.log = log
        self.outcomes: List[Outcome] = []
        self.head: Optional[state.Head] = None
        self.sequence = counter_token.Sequence()
        self.counter_source = ""
        self.run_id = ""
        self.backup_name = ""
        self._lock = None
        self._days: Dict[Tuple[str, str], Tuple[DayAlloc, str]] = {}    # preview overlay
        self._holdings: Dict[str, Holdings] = {}
        self._marks: Dict[str, Dict[str, str]] = {}

    # -- lifecycle -----------------------------------------------------------------

    def __enter__(self) -> "Session":
        self._lock = state.lock(self.command)
        self._lock.__enter__()
        try:
            self.head = state.require_head()
            if not self.preview:
                state.recover(self.head, self.log)
                self.head = state.require_head()
            issued, self.counter_source = state.global_counter(self.head)
            self.sequence = counter_token.Sequence(issued)
            self.run_id = state.new_run_id(self.label, self.mode)
            if not self.preview:
                facets = {"command": self.command, "mode": self.mode, "reason": self.reason,
                          "operator": state.operator(), "counter_before": issued}
                if self.mode == FILL:
                    archive, pruned = state.backup(self.run_id)
                    self.backup_name = archive.name
                    facets.update({"backup": archive.name, "backups_pruned": pruned})
                    self.log(f"  backup: {archive}")
                state.append_event(state.event("START", self.run_id, self.command, facets))
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if not self.preview and self.run_id:
                facets = {"counter_after": self.sequence.issued,
                          "outcomes": [o.__dict__ for o in self.outcomes]}
                if exc is not None:
                    facets["error"] = f"{exc_type.__name__}: {exc}"
                state.append_event(state.event("FAIL" if exc is not None else "COMPLETE",
                                               self.run_id, self.command, facets))
        finally:
            self._lock.__exit__(None, None, None)
        return False

    @property
    def plans_preview(self) -> bool:
        """A --dates --dry-run: normalizers read inputs and plan, write nothing.

        A plain live --dry-run keeps its old meaning (return before reading)."""
        return self.preview and self.mode == FILL

    def fail(self, error: str) -> None:
        """Close the run as FAIL without an exception (strict --dates, a gate)."""
        if not self.preview and self.run_id:
            state.append_event(state.event("FAIL", self.run_id, self.command,
                                           {"error": error, "counter_after": self.sequence.issued,
                                            "outcomes": [o.__dict__ for o in self.outcomes]}))
            self.run_id = ""                     # __exit__ must not also close it

    def record(self, date: str, mic: str, status: str, detail: str = "") -> None:
        self.outcomes.append(Outcome(date, mic.upper(), status, detail))

    def no_input(self, date: str, mic: str, detail: str = "") -> None:
        self.record(date, mic, "no-input", detail or "no input files for this date")

    # -- what is known, overlay first ------------------------------------------------

    def _venue_head(self, mic: str) -> Optional[state.VenueHead]:
        return self.head.venues.get(mic) if self.head is not None else None

    def holdings(self, mic: str) -> Optional[Holdings]:
        if mic in self._holdings:
            return self._holdings[mic].copy()
        vh = self._venue_head(mic)
        return state.load_snapshot(mic, vh) if vh is not None else None

    def days(self, mic: str) -> List[str]:
        planned = {d for (d, m) in self._days if m == mic}
        return sorted(set(state.venue_days(mic)) | planned)

    def day(self, date: Optional[str], mic: str) -> Optional[DayAlloc]:
        if date is None:
            return None
        if (date, mic) in self._days:
            return self._days[(date, mic)][0]
        return state.day_alloc(date, mic)

    def mode_of(self, date: str, mic: str) -> str:
        if (date, mic) in self._days:
            return self._days[(date, mic)][1]
        header = counter_token._read_json(counter_token.venue_manifest_path(date, mic))
        return (header.get("numbering") or {}).get("mode", LIVE)

    def live_date(self, mic: str) -> str:
        if mic in self._marks:
            return self._marks[mic].get("live_date", "")
        vh = self._venue_head(mic)
        return vh.live_date if vh is not None else ""

    # -- the guards ------------------------------------------------------------------

    def venue(self, date: str, mic: str, venue_id: int) -> "VenueNumbering":
        """Resolve what `date` is numbered from, or refuse. Nothing is written."""
        mic = mic.upper()
        holdings = self.holdings(mic)
        if holdings is not None and holdings.venue_id != venue_id:
            raise NumberingRefused(
                f"{mic}: venue_id is {venue_id} in config.ini but {holdings.venue_id} in "
                f"the state. Refusing to renumber a venue under a different id.")
        known = self.days(mic)
        if self.mode == LIVE:
            if holdings is None and known:
                raise NumberingRefused(
                    f"{mic}: has numbered days on disk ({known[-1]} newest) but no "
                    f"state. It was numbered outside a numbering session; refusing to "
                    f"start it from scratch.")
            newest = known[-1] if known else None
            if newest is not None and date < newest:
                raise NumberingRefused(
                    f"{mic}: {date} is older than {newest}, which is already numbered. "
                    f"Numbering it as a live day would fork the chain; fill it with "
                    f"`normalize --dates={date} --venue {mic} --reason ...`.")
            if newest == date and self.mode_of(date, mic) == FILL:
                raise NumberingRefused(
                    f"{mic}: {date} was filled from its neighbours; it cannot be "
                    f"re-numbered as a live day. Run live on a later date.")
            previous = max((d for d in known if d < date), default=None)
            return VenueNumbering(self, date, mic, venue_id, LIVE, holdings,
                                  previous=self.day(previous, mic))
        live_date = self.live_date(mic)
        if live_date and date >= live_date:
            raise NumberingRefused(
                f"{mic}: {date} is not older than {live_date}, the newest live day. "
                f"--dates fills older days; number it live with --date-dir.")
        own = self.day(date, mic) if date in known else None
        others = [d for d in known if d != date]
        earlier, later = numbering.neighbours(date, others)
        earlier = earlier if earlier is not None and _within_window(earlier, date) else None
        later = later if later is not None and _within_window(date, later) else None
        return VenueNumbering(self, date, mic, venue_id, FILL, holdings, own=own,
                              earlier=self.day(earlier, mic), later=self.day(later, mic))


class VenueNumbering:
    """One venue-day inside a session: plan, reserve, commit."""

    def __init__(self, session: Session, date: str, mic: str, venue_id: int, mode: str,
                 holdings: Optional[Holdings], previous: Optional[DayAlloc] = None,
                 own: Optional[DayAlloc] = None, earlier: Optional[DayAlloc] = None,
                 later: Optional[DayAlloc] = None):
        self.session, self.date, self.mic, self.venue_id, self.mode = \
            session, date, mic, venue_id, mode
        self.holdings, self.previous = holdings, previous
        self.own, self.earlier, self.later = own, earlier, later

    @property
    def preview(self) -> bool:
        return self.session.preview

    def describe(self) -> str:
        """Where this venue-day's tokens come from, for the log."""
        if self.mode == LIVE:
            return ("continuing the state" if self.holdings is not None else "first day") + (
                f", previous numbered day {self.previous.date}" if self.previous else "")
        parts = []
        if self.own is not None:
            parts.append("re-run of its own fill")
        if self.later is not None:
            parts.append(f"later {self.later.date}")
        if self.earlier is not None:
            parts.append(f"earlier {self.earlier.date}")
        return "filled from " + (", ".join(parts) or "nothing -- every number new")

    def plan(self, scripts) -> Plan:
        sequence = self.session.sequence
        if self.mode == LIVE:
            return numbering.allocate_live(self.date, scripts, self.holdings, self.previous,
                                           sequence, self.venue_id)
        return numbering.allocate_fill(self.date, scripts, self.own, self.earlier,
                                       self.later, self.holdings, sequence, self.venue_id)

    def reserve(self, plan: Plan) -> None:
        """Raise the day's sequence file to the counter before anything is published."""
        if self.preview:
            return
        existing = counter_token.load_sequence(self.date) or 0
        if self.session.sequence.issued > existing:
            counter_token.write_sequence(self.date, self.session.sequence)

    def summary(self, plan: Plan) -> str:
        c = plan.counts
        return (f"{c.get('scripts', 0):,} scripts, {c.get('drawn', 0):,} new number(s), "
                f"{c.get('exceptions', 0):,} exception(s), {c.get('retained', 0):,} "
                f"retained, counter {plan.counter_before:,} -> {plan.counter_after:,}")

    def commit(self, plan: Plan, started_at: str = "", inputs=(), outputs=()):
        """Stage, commit the head, promote, write the header, log. See the module."""
        session = self.session
        if self.preview:
            session._days[(self.date, self.mic)] = (plan.day, self.mode)
            session._holdings[self.mic] = plan.state_after.copy()
            marks = session._marks.setdefault(self.mic, {"live_date": session.live_date(self.mic)})
            if self.mode == LIVE:
                marks["live_date"] = max(marks.get("live_date", ""), self.date)
            session.record(self.date, self.mic, "previewed", self.summary(plan))
            session.log(f"    PREVIEW {self.date} {self.mic} [{self.mode}] {self.describe()}: "
                        f"{self.summary(plan)}")
            return None

        root = paths.data_root()
        rel = lambda path: str(path.relative_to(root))          # noqa: E731
        run_id = state.new_run_id(self.date, self.mode)
        tokens = counter_token.VenueTokens(self.venue_id, dict(plan.day.assigned),
                                           list(plan.day.free), dict(plan.day.retained))
        staged: List[dict] = []

        alloc_final = counter_token.alloc_path(self.date, self.mic)
        alloc_staged = alloc_final.with_name(alloc_final.name + state.STAGED)
        counter_token.write_alloc(alloc_staged, tokens)
        alloc_sha = counter_token.sha256_of(alloc_staged)
        alloc_bytes = alloc_staged.stat().st_size
        staged.append({"staged": rel(alloc_staged), "final": rel(alloc_final),
                       "sha256": alloc_sha})

        exceptions_block = {"path": "", "sha256": "", "count": 0}
        exceptions_final = counter_token.exceptions_path(self.date, self.mic)
        if plan.exceptions:
            exceptions_staged = exceptions_final.with_name(exceptions_final.name + state.STAGED)
            counter_token.write_exceptions(exceptions_staged,
                                           [e.as_row() for e in plan.exceptions])
            digest = counter_token.sha256_of(exceptions_staged)
            staged.append({"staged": rel(exceptions_staged), "final": rel(exceptions_final),
                           "sha256": digest})
            exceptions_block = {"path": exceptions_final.name, "sha256": digest,
                                "count": len(plan.exceptions)}

        vh = session._venue_head(self.mic)
        snapshot, snapshot_sha, snapshot_rows = state.write_snapshot(
            self.mic, run_id, plan.state_after, current=vh, staged=True)
        if vh is None or snapshot != vh.snapshot:
            final = state.snapshot_path(snapshot)
            staged.append({"staged": rel(final) + state.STAGED, "final": rel(final),
                           "sha256": snapshot_sha})

        numbering_block = {
            "mode": self.mode, "run_id": run_id, "session": session.run_id,
            "neighbours": plan.neighbours,
            "state_before": {"snapshot": vh.snapshot if vh else "",
                             "sha256": vh.sha256 if vh else ""},
            "state_after": {"snapshot": snapshot, "sha256": snapshot_sha},
            "counter_before": plan.counter_before, "counter_after": plan.counter_after,
            "counts": plan.counts, "retired": len(plan.retired),
            "exceptions": exceptions_block,
            "reason": session.reason, "operator": state.operator(),
        }
        carried = (plan.neighbours.get("later") or plan.neighbours.get("earlier") or "")
        run = counter_token.RunStats(
            day=dict(plan.counts), carried_from=carried,
            drawn=plan.counter_after - plan.counter_before,
            sequence_before=plan.counter_before, sequence_after=plan.counter_after,
            anchored_on="state" if self.mode == LIVE else (self.date if self.own else carried),
            sequence_from=session.counter_source)
        payload = counter_token.header_payload(
            self.date, self.mic, tokens, alloc_final.name, alloc_bytes, alloc_sha,
            started_at, run, inputs, outputs, numbering_block)

        head = session.head
        newest = vh.newest if vh else ""
        newest_mode = vh.newest_mode if vh else ""
        if self.date >= newest:
            newest, newest_mode = self.date, self.mode
        live_date = vh.live_date if vh else ""
        if self.mode == LIVE:
            live_date = max(live_date, self.date)
        head.counter = max(head.counter, session.sequence.issued)
        head.venues[self.mic] = state.VenueHead(
            self.venue_id, snapshot, snapshot_sha, snapshot_rows, newest=newest,
            newest_mode=newest_mode, live_date=live_date, run_id=run_id,
            updated_at=counter_token.utc_now())
        head.last_run = {"run_id": run_id, "session": session.run_id,
                         "command": session.command, "date": self.date, "venue": self.mic,
                         "mode": self.mode, "reason": session.reason,
                         "operator": state.operator(), "completed_at": counter_token.utc_now()}
        head.last_commit = {"run_id": run_id, "date": self.date, "mic": self.mic,
                            "staged": staged, "header": payload}
        state.write_head(head)                                   # 4. COMMIT

        for item in staged:                                      # 5. promote
            os.replace(root / item["staged"], root / item["final"])
        if not plan.exceptions and exceptions_final.exists():
            exceptions_final.unlink()                            # a stale earlier run's
        header = counter_token.write_header(self.date, self.mic, payload)   # 6.

        facets = dict(numbering_block, date=self.date, venue=self.mic,
                      retired_tokens=plan.retired[:1000])
        state.append_event(state.event(                          # 7.
            "COMPLETE", run_id, session.command, facets,
            inputs=[a.as_dict() for a in inputs], outputs=[a.as_dict() for a in outputs],
            parent=session.run_id))
        session.record(self.date, self.mic, "done", self.summary(plan))
        return header
