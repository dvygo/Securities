"""The two allocators (premarketv6/normalize/numbering.py), rule by rule and then
against generated histories.

Rule tests use the worked examples from the design discussion so a failure reads
as a story. The property test at the bottom builds random instrument universes
(listings and expiries every day), numbers them through random schedules of
live runs, fills and re-runs, and checks the whole contract after each history.
It found two design bugs before any of this code existed; it stays so that it
finds the next one.
"""
import random
import zlib

import pytest

from premarketv6.normalize import numbering as n
from premarketv6.normalize.counter_token import Sequence


def _holdings(assigned, date, free=()):
    return n.Holdings(1, dict(assigned), sorted(free), {s: date for s in assigned})


def _day(date, assigned, retained=None, free=()):
    return n.DayAlloc(date, dict(assigned), dict(retained or {}), sorted(free))


class TestFillAllocation:
    """allocate_fill: an older day filled from the numbered days either side."""

    def test_backward_fill_keeps_the_later_days_tokens(self):
        """History before the first day. C keeps its token; A and F, gone by the
        25th, take new numbers because nothing is free."""
        state = _holdings({"B": 5, "C": 6, "E": 8}, "20260925")
        plan = n.allocate_fill("20260924", ["A", "C", "F"], None, None,
                               _day("20260925", {"B": 5, "C": 6, "E": 8}),
                               state, Sequence(8), 1)
        assert plan.day.assigned == {"A": 9, "C": 6, "F": 10}
        assert plan.exceptions == []
        assert plan.neighbours == {"earlier": "", "later": "20260925"}

    def test_a_fill_adds_holdings_and_changes_none(self):
        """Rule 5: the state after a fill is the state before, plus holdings for
        scripts that held nothing. B and E keep their tokens for the next live day."""
        state = _holdings({"B": 5, "C": 6, "E": 8}, "20260925")
        plan = n.allocate_fill("20260924", ["A", "C", "F"], None, None,
                               _day("20260925", {"B": 5, "C": 6, "E": 8}),
                               state, Sequence(8), 1)
        for script, token in state.assigned.items():
            assert plan.state_after.assigned[script] == token
        assert plan.state_after.assigned["A"] == 9 and plan.state_after.assigned["F"] == 10
        assert plan.state_after.last_date["A"] == "20260924"
        assert plan.state_after.last_date["B"] == "20260925"

    def test_a_clash_goes_to_the_later_day_and_is_recorded(self):
        """Tuesday M=7; Thursday, M gone, X listed on Wednesday took 7. Filling
        Wednesday, both are present: X keeps 7, M gets a new number, on record."""
        tue = _day("20260922", {"K": 3, "M": 7})
        thu = _day("20260924", {"K": 3, "X": 7})
        state = _holdings({"K": 3, "X": 7}, "20260924")
        plan = n.allocate_fill("20260923", ["K", "M", "X"], None, tue, thu,
                               state, Sequence(7), 1)
        assert plan.day.assigned == {"K": 3, "M": 8, "X": 7}
        [exc] = plan.exceptions
        assert exc.as_row() == {"script": "M", "token": 8, "wanted": 7,
                                "wanted_from": "20260922", "lost_to": "X"}

    def test_neighbours_that_disagree_are_resolved_to_the_later_one_and_recorded(self):
        tue = _day("20260922", {"S": 4})
        thu = _day("20260924", {"S": 9})
        plan = n.allocate_fill("20260923", ["S"], None, tue, thu,
                               _holdings({"S": 9}, "20260924"), Sequence(9), 1)
        assert plan.day.assigned == {"S": 9}
        [exc] = plan.exceptions
        assert (exc.wanted, exc.wanted_from, exc.lost_to) == (4, "20260922", "chain:20260924")

    def test_with_only_an_earlier_neighbour_it_carries_forward(self):
        plan = n.allocate_fill("20260923", ["A", "B"], None,
                               _day("20260922", {"A": 1}), None,
                               _holdings({"A": 1}, "20260922"), Sequence(1), 1)
        assert plan.day.assigned == {"A": 1, "B": 2}

    def test_with_no_neighbour_and_no_state_everything_is_new(self):
        plan = n.allocate_fill("20260920", ["A", "B"], None, None, None, None,
                               Sequence(138_744), 12)
        assert plan.day.assigned == {"A": 138_745, "B": 138_746}
        assert plan.state_after.assigned == plan.day.assigned

    def test_a_rerun_of_a_filled_day_changes_nothing_and_draws_nothing(self):
        state = _holdings({"B": 5, "C": 6, "E": 8}, "20260925")
        later = _day("20260925", {"B": 5, "C": 6, "E": 8})
        seq = Sequence(8)
        first = n.allocate_fill("20260924", ["A", "C", "F"], None, None, later, state, seq, 1)
        issued = seq.issued
        again = n.allocate_fill("20260924", ["A", "C", "F"], first.day, None, later,
                                first.state_after, seq, 1)
        assert again.day.assigned == first.day.assigned
        assert seq.issued == issued
        assert again.state_after.assigned == first.state_after.assigned

    def test_a_script_added_on_rerun_still_takes_its_neighbours_token(self):
        """Review finding R4: the day's own manifest is the source only for its
        own scripts. B, missing from the first fill, is on the later day -- it
        must take the later day's token, not a new one."""
        state = _holdings({"B": 5, "C": 6}, "20260925")
        later = _day("20260925", {"B": 5, "C": 6})
        seq = Sequence(6)
        first = n.allocate_fill("20260924", ["C"], None, None, later, state, seq, 1)
        again = n.allocate_fill("20260924", ["B", "C"], first.day, None, later,
                                first.state_after, seq, 1)
        assert again.day.assigned == {"B": 5, "C": 6}
        assert again.exceptions == []

    def test_a_dropped_script_on_rerun_is_retained_for_the_date(self):
        state = _holdings({"C": 6}, "20260925")
        later = _day("20260925", {"C": 6})
        seq = Sequence(6)
        first = n.allocate_fill("20260924", ["A", "C"], None, None, later, state, seq, 1)
        again = n.allocate_fill("20260924", ["C"], first.day, None, later,
                                first.state_after, seq, 1)
        assert again.day.retained == {"A": first.day.assigned["A"]}
        assert first.day.assigned["A"] not in again.day.free

    def test_a_relisted_script_uses_its_state_token(self):
        """R on the 23rd and in today's state, but not on the 24th: it takes the
        token it holds rather than a new one."""
        state = _holdings({"C": 6, "R": 11}, "20260925")
        plan = n.allocate_fill("20260923", ["C", "R"], None, None,
                               _day("20260924", {"C": 6}), state, Sequence(11), 1)
        assert plan.day.assigned == {"C": 6, "R": 11}

    def test_a_relisted_scripts_taken_token_is_never_overwritten_in_the_state(self):
        """The bug the simulation found. R holds 7 in the state, but on this day 7
        belongs to X (kept from the later day). R takes a day-local number; the
        state must keep R=7, or R's live token changes at the next live run."""
        state = _holdings({"R": 7, "X": 3}, "20260925")
        later = _day("20260924", {"X": 7})
        plan = n.allocate_fill("20260923", ["R", "X"], None, None, later, state,
                               Sequence(11), 1)
        assert plan.day.assigned["X"] == 7 and plan.day.assigned["R"] == 12
        assert plan.state_after.assigned["R"] == 7
        assert plan.retired == [12]

    def test_a_free_token_a_neighbour_hands_to_someone_else_is_skipped(self):
        """Review finding R6: 4 is free in the state but the earlier day gave it to
        Q. Handing it to N here would set up a clash the day a fill lands between."""
        state = _holdings({"K": 3}, "20260925", free=[4, 5])
        plan = n.allocate_fill("20260923", ["K", "N"], None,
                               _day("20260922", {"K": 3, "Q": 4}), None, state,
                               Sequence(9), 1)
        assert plan.day.assigned["N"] == 5
        assert plan.state_after.free == [4]

    def test_a_kept_token_sitting_in_free_is_not_handed_to_anyone_else(self):
        """K's token 3 was released since the later day, so it is free in the
        state. K keeps it here; no other script on this day may take it."""
        state = _holdings({}, "20260925", free=[3, 4])
        plan = n.allocate_fill("20260923", ["K", "Z"], None, None,
                               _day("20260924", {"K": 3}), state, Sequence(9), 1)
        assert plan.day.assigned["K"] == 3 and plan.day.assigned["Z"] == 4


class TestLiveAllocation:
    """allocate_live: the newest day, continued from the latest state."""

    def test_the_first_day_numbers_from_the_counter(self):
        plan = n.allocate_live("20260925", ["B", "A"], None, None, Sequence(138_744), 12)
        assert plan.day.assigned == {"A": 138_745, "B": 138_746}

    def test_the_live_day_after_a_fill_keeps_every_live_token(self):
        """The E example end to end: state after the 24th's fill holds A and F
        too; the 26th keeps B, C, E and releases A and F."""
        state = _holdings({"B": 5, "C": 6, "E": 8}, "20260925")
        live25 = _day("20260925", {"B": 5, "C": 6, "E": 8})
        seq = Sequence(8)
        fill = n.allocate_fill("20260924", ["A", "C", "F"], None, None, live25, state, seq, 1)
        plan = n.allocate_live("20260926", ["B", "C", "D", "E"], fill.state_after,
                               live25, seq, 1)
        assert {s: plan.day.assigned[s] for s in "BCE"} == {"B": 5, "C": 6, "E": 8}
        assert plan.day.assigned["D"] == 9
        assert plan.exceptions == []

    def test_a_same_day_rerun_with_fewer_scripts_retains_rather_than_frees(self):
        """Decision 20: append-only within a date."""
        seq = Sequence()
        first = n.allocate_live("20260925", ["A", "B", "C"], None, None, seq, 1)
        again = n.allocate_live("20260925", ["A", "B"], first.state_after, None, seq, 1)
        assert again.day.retained == {"C": 3}
        assert 3 not in again.day.free and again.state_after.assigned["C"] == 3

    def test_a_mid_day_vendor_switch_never_reuses_a_token_on_that_date(self):
        """NSE numbers the market at 09:00; Fyers, with different coverage, at
        14:00. Nothing Fyers lacks may lose its number to something only Fyers has."""
        seq = Sequence()
        nse = n.allocate_live("20260925", ["A", "B", "N1"], None, None, seq, 1)
        fyers = n.allocate_live("20260925", ["A", "B", "F1"], nse.state_after, None, seq, 1)
        seen = {}
        for plan in (nse, fyers):
            for script, token in plan.day.assigned.items():
                assert seen.setdefault(token, script) == script
        assert fyers.day.assigned["F1"] != nse.day.assigned["N1"]

    def test_a_retained_token_is_freed_on_the_next_date(self):
        seq = Sequence()
        first = n.allocate_live("20260925", ["A", "B", "C"], None, None, seq, 1)
        again = n.allocate_live("20260925", ["A", "B"], first.state_after, None, seq, 1)
        nxt = n.allocate_live("20260926", ["A", "B", "D"], again.state_after,
                              again.day, seq, 1)
        assert nxt.day.assigned["D"] == 3

    def test_a_returning_script_reclaims_and_is_not_an_exception(self):
        seq = Sequence()
        day1 = n.allocate_live("20260924", ["A", "C", "E"], None, None, seq, 1)
        truncated = n.allocate_live("20260925", ["A", "E"], day1.state_after, day1.day, seq, 1)
        rerun = n.allocate_live("20260925", ["A", "B2", "C", "E"], truncated.state_after,
                                day1.day, seq, 1)
        assert rerun.day.assigned["C"] == day1.day.assigned["C"]
        assert rerun.exceptions == []

    def test_a_same_day_rerun_is_idempotent(self):
        seq = Sequence()
        first = n.allocate_live("20260925", ["A", "B"], None, None, seq, 1)
        issued = seq.issued
        again = n.allocate_live("20260925", ["A", "B"], first.state_after, None, seq, 1)
        assert again.day.assigned == first.day.assigned and seq.issued == issued


# -- property test: the simulation that found the design bugs ---------------------

def _universe(rng, days):
    """Listings and expiries every day, plus the untidy part of real feeds: about
    one instrument in ten is missing for a single day mid-life (a vendor hiccup,
    a suspension) and comes back. Those are what exercise relisting and reclaims."""
    listings, gaps, number = {}, {}, 0
    for d in range(days):
        for _ in range(rng.randint(5, 25) if d else 80):
            number += 1
            script = f"S{number:04d}"
            first, last = (d if d else -rng.randint(0, 5)), d + rng.randint(0, 6)
            listings[script] = (first, last)
            if last - first >= 2 and rng.random() < 0.1:
                gaps[script] = rng.randint(first + 1, last - 1)
    return lambda d: sorted(s for s, (a, b) in listings.items()
                            if a <= d <= b and gaps.get(s) != d)


def _date(d):
    return f"202609{d + 1:02d}"


def _run_history(seed, schedule, days=14):
    rng = random.Random(seed)
    on = _universe(rng, days)
    sequence = Sequence()
    state, days_done, exceptions = None, {}, {}
    issued, handed = [], {}          # handed: date -> {token: script}, across every run
    problems = []

    def record(date, plan):
        for script, token in plan.day.assigned.items():
            if handed.setdefault(date, {}).setdefault(token, script) != script:
                problems.append(f"rule 1: {date} token {token} handed to two scripts")

    for op, d in schedule:
        date, before = _date(d), sequence.issued
        scripts = on(d)
        if op == "switch":                          # same date, other coverage
            scripts = [s for s in scripts if zlib.crc32(f"{seed}:{s}".encode()) % 5]
        if op in ("live", "switch"):
            numbered = sorted(days_done)
            previous = max((x for x in numbered if x < date), default=None)
            prior_state = state.copy() if state is not None else None
            plan = n.allocate_live(date, scripts, state, days_done.get(previous), sequence, 1)
            if prior_state is not None and op == "live":
                for s in set(scripts) & set(prior_state.assigned):
                    if plan.day.assigned[s] != prior_state.assigned[s]:
                        problems.append(f"rule 5: live {date} moved held {s}")
        else:
            numbered = sorted(x for x in days_done if x != date)
            earlier, later = n.neighbours(date, numbered)
            prior_state = state.copy() if state is not None else None
            plan = n.allocate_fill(date, scripts, days_done.get(date),
                                   days_done.get(earlier), days_done.get(later),
                                   state, sequence, 1)
            if prior_state is not None:
                for s, t in prior_state.assigned.items():
                    if plan.state_after.assigned.get(s) != t:
                        problems.append(f"rule 5: fill {date} altered held {s}")
            if op == "refill" and sequence.issued != before:
                problems.append(f"rule 4: re-run of {date} drew numbers")
        record(date, plan)
        issued.extend(range(before + 1, sequence.issued + 1))
        state, days_done[date] = plan.state_after, plan.day
        exceptions[date] = plan.exceptions
        if set(state.free) & set(state.assigned.values()):
            problems.append(f"state overlap after {date}")

    if len(issued) != len(set(issued)):
        problems.append("rule 3: a number was issued twice")
    ordered = sorted(days_done)
    for x, y in zip(ordered, ordered[1:]):
        a, b = days_done[x].assigned, days_done[y].assigned
        allowed = ({e.script for e in exceptions.get(y, []) if e.wanted_from == x}
                   | {e.script for e in exceptions.get(x, []) if e.wanted_from == y})
        moved = {s for s in set(a) & set(b) if a[s] != b[s]}
        if moved - allowed:
            problems.append(f"rule 2: {x}->{y} unrecorded {sorted(moved - allowed)[:3]}")
        if (allowed & set(a) & set(b)) - moved:
            problems.append(f"rule 2: {x}->{y} recorded but not moved")
    return problems


SCHEDULES = {
    "backward newest-first before live":
        [("live", 7)] + [("fill", d) for d in (6, 5, 4, 3, 2, 1, 0)]
        + [("live", d) for d in range(8, 14)],
    "gaps filled newest-first":
        [("live", d) for d in (0, 1, 2, 4, 5, 8, 9, 10)]
        + [("fill", 7), ("fill", 6), ("fill", 3)] + [("live", d) for d in (11, 12, 13)],
    "gaps filled oldest-first":
        [("live", d) for d in (0, 1, 2, 4, 5, 8, 9, 10)]
        + [("fill", 3), ("fill", 6), ("fill", 7)] + [("live", d) for d in (11, 12, 13)],
    "gap filled across separate runs":
        [("live", d) for d in (0, 1, 2, 5, 6)]
        + [("fill", 4), ("live", 7), ("fill", 3), ("live", 8)],
    "fills interleaved with live runs":
        [("live", 5), ("fill", 4), ("live", 6), ("fill", 3), ("live", 8), ("fill", 7),
         ("fill", 2), ("live", 9), ("fill", 1), ("live", 10)],
    "re-runs of filled days":
        [("live", 5), ("fill", 4), ("fill", 3), ("live", 6), ("refill", 4), ("refill", 3),
         ("live", 7), ("refill", 4)],
    "mid-day vendor switches, then a backfill":
        [("live", 1), ("switch", 1), ("live", 1), ("live", 2), ("switch", 2),
         ("fill", 0), ("live", 3), ("switch", 3)],
}


def _broken(schedule, seeds=range(60)):
    return {seed: p for seed in seeds if (p := _run_history(seed, schedule))}


@pytest.mark.parametrize("name", sorted(SCHEDULES))
def test_the_contract_holds_across_generated_histories(name):
    failures = _broken(SCHEDULES[name])
    assert not failures, f"{name}: {len(failures)} of 60 histories broke the contract: " \
                         f"{dict(list(failures.items())[:2])}"


# -- mutation: prove the property test is not vacuous ------------------------------

def test_the_property_test_catches_unrecorded_breaks(monkeypatch):
    """Silence the exception records and the gap schedules must fail rule 2."""
    monkeypatch.setattr(n, "breaks", lambda *a, **k: [])
    failures = _broken(SCHEDULES["gaps filled newest-first"])
    assert failures and any("rule 2" in p for ps in failures.values() for p in ps)


def test_the_property_test_catches_a_fill_that_overwrites_a_holding(monkeypatch):
    """Re-introduce the bug the simulation found: a fill that records every
    step-4 number as a holding, overwriting the one the script already had."""
    original = n.allocate_fill

    def overwriting(*args, **kwargs):
        plan = original(*args, **kwargs)
        for script, token in plan.day.assigned.items():
            if script in plan.state_after.assigned and token in plan.retired:
                plan.state_after.assigned[script] = token
        return plan

    monkeypatch.setattr(n, "allocate_fill", overwriting)
    failures = {}
    for name in ("gaps filled oldest-first", "gaps filled newest-first",
                 "fills interleaved with live runs"):
        failures.update(_broken(SCHEDULES[name], range(200)))
    assert any("rule 5" in p for ps in failures.values() for p in ps)
