"""The numbering session (premarketv6/normalize/numbering_session.py) and the state store's
operational half: init-state, the lock, the run log, backups and recovery.

These drive the session the way a normalizer does -- venue(), plan(), reserve(),
commit() -- against a throwaway data root, and then read back what a later run,
check-tokens or an auditor would read.
"""
import json
import tarfile

import pytest

from premarketv6 import paths
from premarketv6.normalize import counter_token, state
from premarketv6.normalize.numbering import FILL, LIVE
from premarketv6.normalize.numbering_session import NumberingRefused, Session

CONFIG = """[paths]
data_dir = {root}

[EXCHANGE:XCME]
feed = databento
enabled = 1
venue_id = 12
dataset = GLBX.MDP3

[EXCHANGE:XNSE]
feed = fyers
enabled = 1
venue_id = 16
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A data root with 20260925's XNSE numbered 1..3, the way the box is today."""
    monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "config.ini").write_text(CONFIG.format(root=tmp_path))
    monkeypatch.setattr(paths, "config_ini", lambda: conf / "config.ini")
    from premarketv6 import config
    config_cache = getattr(config, "load_exchanges", None)
    if hasattr(config_cache, "cache_clear"):
        config_cache.cache_clear()
    counter_token._exchanges.cache_clear()
    counter_token.write_sequence("20260925", counter_token.Sequence(3))
    counter_token.write_venue_manifest(
        "20260925", "XNSE", counter_token.VenueTokens(16, {"N1": 1, "N2": 2, "N3": 3}, []))
    return tmp_path


def _quiet(*_):
    pass


def _init(reason="test"):
    return state.init_state(reason, log=_quiet)


def _number(mode, date, mic, venue_id, scripts, reason="test", **kw):
    """One venue-day through a whole session, the way a normalizer drives it."""
    with Session("normalize", mode, reason=reason, log=_quiet, **kw) as session:
        venue = session.venue(date, mic, venue_id)
        plan = venue.plan(scripts)
        venue.reserve(plan)
        venue.commit(plan, counter_token.utc_now())
    return plan, session


class TestInitState:
    def test_it_builds_the_head_from_the_newest_manifests(self, tree):
        head = _init()
        assert head.counter == 3
        assert head.venues["XNSE"].newest == "20260925"
        assert head.venues["XNSE"].live_date == "20260925"
        assert state.load_snapshot("XNSE", head.venues["XNSE"]).assigned == \
            {"N1": 1, "N2": 2, "N3": 3}
        assert "XCME" not in head.venues          # never numbered: first live run makes it

    def test_it_runs_once(self, tree):
        _init()
        with pytest.raises(state.StateCorrupt, match="already exists"):
            _init()

    def test_a_dry_run_writes_nothing(self, tree):
        assert state.init_state("test", dry_run=True, log=_quiet) is None
        assert not state.state_dir().exists()

    def test_it_refuses_venues_that_share_a_token(self, tree):
        """Review finding R5: a day numbered in isolation (the old code, from 1)
        would otherwise become every future day's collision."""
        counter_token.write_venue_manifest(
            "20260924", "XCME", counter_token.VenueTokens(12, {"C1": 2}, []))
        with pytest.raises(state.StateCorrupt, match="share 1 token"):
            _init()

    def test_it_takes_a_backup_and_logs_the_run(self, tree):
        _init("adopt the state store")
        assert len(list(state.backups_dir().glob("*.tar.gz"))) == 1
        kinds = [e["eventType"] for e in state.read_events()]
        assert kinds == ["START", "COMPLETE"]
        assert state.read_events()[-1]["run"]["facets"]["premarketv6"]["reason"] == \
            "adopt the state store"


class TestSessionGuards:
    def test_no_head_means_no_numbering(self, tree):
        with pytest.raises(state.StateMissing, match="init-state"):
            with Session("normalize", LIVE, log=_quiet):
                pass

    def test_a_second_session_is_refused_while_one_runs(self, tree):
        _init()
        with Session("normalize", LIVE, log=_quiet):
            with pytest.raises(state.LockHeld, match="another numbering run"):
                with Session("normalize", LIVE, log=_quiet):
                    pass

    def test_live_refuses_a_date_older_than_the_newest_numbered_day(self, tree):
        """Review finding R3, and the collision this whole branch prevents."""
        _init()
        with Session("normalize", LIVE, log=_quiet) as session:
            with pytest.raises(NumberingRefused, match="--dates"):
                session.venue("20260924", "XNSE", 16)

    def test_fill_refuses_a_date_that_is_not_older_than_the_live_day(self, tree):
        _init()
        with Session("normalize", FILL, reason="t", log=_quiet) as session:
            with pytest.raises(NumberingRefused, match="--date-dir"):
                session.venue("20260925", "XNSE", 16)

    def test_live_refuses_a_filled_newest_day(self, tree):
        """XCME is filled but never live: a live run on that same date would
        renumber a filled day as if it were the live chain."""
        _init()
        _number(FILL, "20260924", "XCME", 12, ["C1", "C2"])
        with Session("normalize", LIVE, log=_quiet) as session:
            with pytest.raises(NumberingRefused, match="filled"):
                session.venue("20260924", "XCME", 12)
            with pytest.raises(NumberingRefused, match="older"):
                session.venue("20260923", "XCME", 12)

    def test_a_changed_venue_id_is_refused(self, tree):
        _init()
        with Session("normalize", LIVE, log=_quiet) as session:
            with pytest.raises(NumberingRefused, match="venue_id"):
                session.venue("20260926", "XNSE", 99)


class TestTheCollisionOnThisBox:
    """The reason for the branch, end to end: 20260925 numbered first, 20260924
    filled after, then today's XCME live. No token may be shared on a date."""

    def test_backfilled_xcme_never_takes_a_number_xnse_holds(self, tree):
        _init()
        fill, _ = _number(FILL, "20260924", "XCME", 12, ["C1", "C2", "C3"])
        assert min(fill.day.assigned.values()) > 3
        live, _ = _number(LIVE, "20260925", "XCME", 12, ["C1", "C2", "C4"])
        xnse = set(counter_token.venue_entry("20260925", "XNSE")["assigned"].values())
        assert not (set(live.day.assigned.values()) & xnse)
        assert live.day.assigned["C1"] == fill.day.assigned["C1"]
        assert len(set(live.day.assigned.values())) == 3


class TestCommitProtocol:
    def test_a_live_run_writes_every_artifact_and_logs_it(self, tree):
        _init()
        plan, session = _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N4"])
        assert plan.day.assigned["N4"] == 3       # N3 left; its number is reused, not drawn
        entry = counter_token.venue_entry("20260926", "XNSE")
        assert entry["assigned"] == plan.day.assigned
        header = json.loads(counter_token.venue_manifest_path("20260926", "XNSE").read_text())
        block = header["numbering"]
        assert block["mode"] == LIVE and block["reason"] == "test"
        head = state.read_head()
        assert head.venues["XNSE"].snapshot == block["state_after"]["snapshot"]
        assert head.venues["XNSE"].live_date == "20260926"
        assert head.counter == counter_token.load_sequence("20260926") == 3
        assert not list(tree.rglob("*.staged"))
        kinds = [(e["eventType"], e["job"]["name"]) for e in state.read_events()]
        assert kinds[-3:] == [("START", "normalize"), ("COMPLETE", "normalize"),
                              ("COMPLETE", "normalize")]
        assert session.outcomes[0].status == "done"

    def test_a_fill_records_its_exceptions_beside_the_allocation(self, tree):
        """The M/N clash, through the whole session."""
        _init()
        _number(LIVE, "20260926", "XCME", 12, ["K", "M"])       # 26th: K, M
        _number(LIVE, "20260928", "XCME", 12, ["K", "X"])       # 28th: M gone, X takes M's
        m = counter_token.venue_entry("20260926", "XCME")["assigned"]["M"]
        x = counter_token.venue_entry("20260928", "XCME")["assigned"]["X"]
        assert m == x
        plan, _ = _number(FILL, "20260927", "XCME", 12, ["K", "M", "X"], reason="missed day")
        rows = counter_token.read_exceptions("20260927", "XCME")
        assert [(r["script"], r["wanted_from"], r["lost_to"]) for r in rows] == \
            [("M", "20260926", "X")]
        assert plan.day.assigned["X"] == x and plan.day.assigned["M"] != m

    def test_a_fill_takes_a_backup_first(self, tree):
        _init()
        before = len(list(state.backups_dir().glob("*.tar.gz")))
        _number(FILL, "20260924", "XCME", 12, ["C1"])
        assert len(list(state.backups_dir().glob("*.tar.gz"))) == before + 1

    def test_backups_are_pruned_to_the_newest_ten(self, tree):
        _init()
        for day in range(10, 22):
            _number(FILL, f"202609{day:02d}", "XCME", 12, ["C1"])
        assert len(list(state.backups_dir().glob("*.tar.gz"))) == state.BACKUPS_KEEP

    def test_a_backup_restores_the_manifests_it_took(self, tree):
        _init()
        header = counter_token.venue_manifest_path("20260925", "XNSE")
        original = header.read_bytes()
        archive, _ = state.backup("manual")
        header.write_text("{}")
        with tarfile.open(archive) as tar:
            tar.extractall(tree, filter="data")
        assert header.read_bytes() == original

    def test_a_rerun_that_changes_nothing_reuses_the_snapshot(self, tree):
        _init()
        _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2"])
        first = state.read_head().venues["XNSE"].snapshot
        _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2"])
        assert state.read_head().venues["XNSE"].snapshot == first


class TestCrashRecovery:
    """Crash at each step of commit(), then the next session recovers.

    Each crash is patched inside monkeypatch.context() so that undoing it does
    not also undo the fixture's temporary data root."""

    @staticmethod
    def _crashing(m, target, name):
        def boom(*a, **k):
            raise RuntimeError(f"simulated crash in {name}")
        m.setattr(target, name, boom)

    def test_a_crash_before_the_commit_leaks_numbers_and_nothing_else(self, tree, monkeypatch):
        _init()
        everyone = ["N1", "N2", "N3", "N4", "N5"]            # N4, N5 must draw 4, 5
        with monkeypatch.context() as m:
            self._crashing(m, state, "write_head")
            with pytest.raises(RuntimeError, match="simulated"):
                _number(LIVE, "20260926", "XNSE", 16, everyone)
        assert list(tree.rglob("*.staged"))                   # staged, never committed
        assert counter_token.load_sequence("20260926") == 5   # reserved before publishing
        plan, _ = _number(LIVE, "20260926", "XNSE", 16, everyone)
        assert not list(tree.rglob("*.staged"))
        assert (plan.day.assigned["N4"], plan.day.assigned["N5"]) == (6, 7)   # 4, 5 leaked
        failed = [e for e in state.read_events() if e["eventType"] == "FAIL"]
        assert len(failed) == 1

    def test_a_crash_after_the_commit_is_finished_by_recovery(self, tree, monkeypatch):
        _init()
        with monkeypatch.context() as m:
            self._crashing(m, counter_token, "write_header")
            with pytest.raises(RuntimeError, match="simulated"):
                _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N3", "N4"])
        head = state.read_head()
        assert head.last_commit["date"] == "20260926"
        assert not counter_token.venue_manifest_path("20260926", "XNSE").exists()
        actions = state.recover(head, log=_quiet)
        assert any("wrote the XNSE 20260926 header" in a for a in actions)
        assert counter_token.venue_entry("20260926", "XNSE")["assigned"] == \
            {"N1": 1, "N2": 2, "N3": 3, "N4": 4}

    def test_the_next_session_recovers_on_its_own(self, tree, monkeypatch):
        """Nobody has to remember to run recovery: every session does it first."""
        _init()
        with monkeypatch.context() as m:
            self._crashing(m, counter_token, "write_header")
            with pytest.raises(RuntimeError, match="simulated"):
                _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N3", "N4"])
        with Session("normalize", LIVE, log=_quiet):
            pass
        assert counter_token.venue_entry("20260926", "XNSE")["assigned"]["N4"] == 4

    def test_a_crash_between_commit_and_promote_is_promoted(self, tree, monkeypatch):
        _init()
        real_replace = state.os.replace
        calls = {"n": 0}

        def fail_first_promote(src, dst):
            committed = '"last_commit": {' in state.head_path().read_text()
            if str(src).endswith(state.STAGED) and committed and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("simulated crash in promote")
            return real_replace(src, dst)

        with monkeypatch.context() as m:
            m.setattr(state.os, "replace", fail_first_promote)
            with pytest.raises(RuntimeError, match="simulated"):
                _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N3", "N4"])
        actions = state.recover(state.read_head(), log=_quiet)
        assert any(a.startswith("promoted") for a in actions)
        assert not list(tree.rglob("*.staged"))
        assert counter_token.venue_entry("20260926", "XNSE")["assigned"]["N4"] == 4

    def test_a_tampered_staged_file_is_never_promoted(self, tree, monkeypatch):
        _init()
        real_replace = state.os.replace

        def never_promote(src, dst):
            if str(src).endswith(state.STAGED) and '"last_commit": {' in \
                    state.head_path().read_text():
                raise RuntimeError("simulated crash in promote")
            return real_replace(src, dst)

        with monkeypatch.context() as m:
            m.setattr(state.os, "replace", never_promote)
            with pytest.raises(RuntimeError):
                _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N3", "N4"])
        head = state.read_head()
        staged = tree / head.last_commit["staged"][0]["staged"]
        staged.write_bytes(b"tampered")
        with pytest.raises(state.StateCorrupt, match="digest"):
            state.recover(head, log=_quiet)


class TestPreview:
    def test_a_preview_writes_nothing_and_chains_its_own_plans(self, tree):
        _init()
        before = sorted(p.relative_to(tree) for p in tree.rglob("*") if p.is_file())
        with Session("normalize", FILL, reason="t", preview=True, log=_quiet) as session:
            for date, scripts in (("20260924", ["C1", "C2"]), ("20260923", ["C1", "C9"])):
                venue = session.venue(date, "XCME", 12)
                plan = venue.plan(scripts)
                venue.reserve(plan)
                venue.commit(plan)
        after = sorted(p.relative_to(tree) for p in tree.rglob("*") if p.is_file()
                       if p.name != ".lock")
        assert after == [p for p in before if p.name != ".lock"]
        assert [o.status for o in session.outcomes] == ["previewed", "previewed"]
        assert session.outcomes[1].detail.startswith("2 scripts")

    def test_a_preview_draws_what_the_real_run_would(self, tree):
        _init()
        with Session("normalize", FILL, reason="t", preview=True, log=_quiet) as session:
            venue = session.venue("20260924", "XCME", 12)
            planned = venue.plan(["C1", "C2"])
        real, _ = _number(FILL, "20260924", "XCME", 12, ["C1", "C2"])
        assert planned.day.assigned == real.day.assigned


# -- the QA tools, held against what the session actually writes ----------------------

def _write_normalized(date, mic, assigned):
    """The normalized parquet a normalizer would have written for this plan."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    directory = paths.normalized_dir(date)
    directory.mkdir(parents=True, exist_ok=True)
    scripts = sorted(assigned)
    pq.write_table(pa.table({
        "script": scripts,
        "counterToken": [str(n) for n in range(1, len(scripts) + 1)],
        "counterTokenV2": [str(assigned[s]) for s in scripts],
    }), directory / f"{mic}-DATABENTO-normalized.parquet")


def _numbered(mode, date, mic, venue_id, scripts, **kw):
    plan, _ = _number(mode, date, mic, venue_id, scripts, **kw)
    _write_normalized(date, mic, plan.day.assigned)
    return plan


def _hard_failures(checks):
    return [f"{c.day} {c.venue} {c.name}: {c.detail}" for c in checks if not c.ok and c.hard]


class TestCheckTokensOnSessionDays:
    def test_a_live_chain_passes(self, tree):
        from premarketv6.qa import tokens as qa
        _init()
        _numbered(LIVE, "20260926", "XCME", 12, ["A", "B", "C"])
        _numbered(LIVE, "20260927", "XCME", 12, ["A", "C", "D"])
        checks = qa.collect(["20260926", "20260927"], ["XCME"])
        assert _hard_failures(checks) == []
        assert any(c.name == "pool drained first" for c in checks)   # against the snapshot

    def test_a_fill_with_a_recorded_clash_passes(self, tree):
        from premarketv6.qa import tokens as qa
        _init()
        _numbered(LIVE, "20260926", "XCME", 12, ["K", "M"])
        _numbered(LIVE, "20260928", "XCME", 12, ["K", "X"])
        _numbered(FILL, "20260927", "XCME", 12, ["K", "M", "X"], reason="missed day")
        checks = qa.collect(["20260926", "20260927", "20260928"], ["XCME"])
        assert _hard_failures(checks) == []
        stable = [c for c in checks if c.name == "stable"]
        assert any("recorded as exceptions" in c.detail for c in stable)
        assert {c.name for c in checks} >= {"never releases", "fill provenance", "filled from"}

    def test_an_unrecorded_move_fails_stable(self, tree):
        from premarketv6.qa import tokens as qa
        _init()
        _numbered(LIVE, "20260926", "XCME", 12, ["A", "B"])
        plan = _numbered(LIVE, "20260927", "XCME", 12, ["A", "B"])
        moved = dict(plan.day.assigned)
        moved["A"], moved["B"] = moved["B"], moved["A"]            # swap two tokens
        _write_normalized("20260927", "XCME", moved)
        failures = _hard_failures(qa.collect(["20260926", "20260927"], ["XCME"]))
        assert any("stable" in f and "no recorded exception" in f for f in failures)

    def test_a_fill_that_alters_a_holding_fails_never_releases(self, tree, monkeypatch):
        """Mutation: a fill whose state_after changes a held token must be caught."""
        from premarketv6.normalize import numbering
        from premarketv6.qa import tokens as qa
        _init()
        _numbered(LIVE, "20260926", "XCME", 12, ["A", "B"])
        original = numbering.allocate_fill

        def altering(*a, **k):
            plan = original(*a, **k)
            plan.state_after.assigned["A"] += 1_000_000
            return plan

        with monkeypatch.context() as m:
            m.setattr(numbering, "allocate_fill", altering)
            _numbered(FILL, "20260925", "XCME", 12, ["A", "B"])
        failures = _hard_failures(qa.check_day("20260925", ["XCME"]))
        assert any("never releases" in f for f in failures)

    def test_pairs_are_built_per_venue(self, tree):
        """XCME missing from the middle date must pair 26->28, not be skipped."""
        from premarketv6.qa import tokens as qa
        for day in ("20260926", "20260928"):
            _write_normalized(day, "XCME", {"A": 1})
        for day in ("20260926", "20260927", "20260928"):
            _write_normalized(day, "XNAS", {"B": 2})
        pairs = qa.pairs_by_venue(["20260926", "20260927", "20260928"])
        assert pairs == {"XCME": ["20260926", "20260928"],
                         "XNAS": ["20260926", "20260927", "20260928"]}


class TestCheckState:
    def test_it_passes_after_normal_runs(self, tree):
        from premarketv6.qa import state_check
        _init()
        _number(LIVE, "20260926", "XNSE", 16, ["N1", "N2", "N9"])
        assert _hard_failures(state_check.collect()) == []

    def test_no_state_is_a_failure(self, tree):
        from premarketv6.qa import state_check
        assert any("init-state" in f for f in _hard_failures(state_check.collect()))

    def test_a_day_numbered_outside_a_session_is_caught(self, tree):
        from premarketv6.qa import state_check
        _init()
        counter_token.write_venue_manifest(
            "20260930", "XNSE", counter_token.VenueTokens(16, {"Q": 99}, []))
        failures = _hard_failures(state_check.collect())
        assert any("newest on disk" in f for f in failures)
        assert any("counter covers" in f for f in failures)

    def test_a_tampered_snapshot_is_caught(self, tree):
        from premarketv6.qa import state_check
        head = _init()
        path = state.snapshot_path(head.venues["XNSE"].snapshot)
        path.write_bytes(path.read_bytes() + b"x")
        assert any("snapshot" in f for f in _hard_failures(state_check.collect()))

    def test_an_unclosed_run_is_a_warning_not_a_failure(self, tree):
        from premarketv6.qa import state_check
        _init()
        state.append_event(state.event("START", "r-open", "normalize", {}))
        [runs] = [c for c in state_check.collect() if c.name == "runs closed"]
        assert not runs.ok and not runs.hard
