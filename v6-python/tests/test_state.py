"""The state store (premarketv6/normalize/state.py): head, snapshots, counter floor."""
import json

import pytest

from premarketv6.normalize import counter_token, state
from premarketv6.normalize.numbering import Holdings


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path))
    return tmp_path


def _holdings():
    return Holdings(10, {"B": 5, "A": 4}, [9, 7], {"A": "20260925", "B": "20260924"})


class TestHead:
    def test_no_head_reads_as_none(self, tree):
        assert state.read_head() is None

    def test_require_head_says_to_run_init_state(self, tree):
        with pytest.raises(state.StateMissing, match="init-state"):
            state.require_head()

    def test_it_round_trips(self, tree):
        head = state.Head(counter=138_744, last_run={"run_id": "r1"})
        head.venues["XNSE"] = state.VenueHead(16, "XNSE/r1.alloc.parquet", "ab", 3,
                                              newest="20260925", newest_mode="live",
                                              live_date="20260925", run_id="r1")
        state.write_head(head)
        again = state.read_head()
        assert again.counter == 138_744
        assert again.venues["XNSE"].live_date == "20260925"
        assert again.last_run == {"run_id": "r1"}

    def test_an_unreadable_head_refuses_rather_than_starting_over(self, tree):
        state.head_path().parent.mkdir(parents=True)
        state.head_path().write_text("{ not json")
        with pytest.raises(state.StateCorrupt, match="Refusing"):
            state.read_head()

    def test_a_head_from_a_future_version_is_refused(self, tree):
        state.head_path().parent.mkdir(parents=True)
        state.head_path().write_text(json.dumps({"version": 99}))
        with pytest.raises(state.StateCorrupt, match="version 99"):
            state.read_head()

    def test_it_is_written_atomically(self, tree):
        state.write_head(state.Head(counter=1))
        assert list(state.state_dir().glob("*.tmp*")) == []


class TestSnapshots:
    def test_it_round_trips(self, tree):
        path, sha, rows = state.write_snapshot("xcme", "r1", _holdings())
        assert path == "XCME/r1.alloc.parquet" and rows == 4
        back = state.load_snapshot("XCME", state.VenueHead(10, path, sha, rows))
        assert back.assigned == {"A": 4, "B": 5}
        assert back.free == [7, 9]
        assert back.last_date == {"A": "20260925", "B": "20260924"}

    def test_the_same_holdings_give_the_same_digest(self, tree):
        _, first, _ = state.write_snapshot("XCME", "r1", _holdings())
        _, second, _ = state.write_snapshot("XCME", "r2", _holdings())
        assert first == second

    def test_an_unchanged_state_reuses_the_current_snapshot(self, tree):
        path, sha, rows = state.write_snapshot("XCME", "r1", _holdings())
        current = state.VenueHead(10, path, sha, rows)
        again = state.write_snapshot("XCME", "r2", _holdings(), current=current)
        assert again == (path, sha, rows)
        assert sorted(p.name for p in (state.state_dir() / "XCME").iterdir()) == \
            ["r1.alloc.parquet"]

    def test_a_snapshot_is_never_overwritten(self, tree):
        state.write_snapshot("XCME", "r1", _holdings())
        changed = _holdings()
        changed.assigned["C"] = 11
        with pytest.raises(state.StateCorrupt, match="never overwritten"):
            state.write_snapshot("XCME", "r1", changed)

    def test_a_tampered_snapshot_is_refused(self, tree):
        path, sha, rows = state.write_snapshot("XCME", "r1", _holdings())
        target = state.snapshot_path(path)
        target.write_bytes(target.read_bytes() + b"x")
        with pytest.raises(state.StateCorrupt, match="changed after it was written"):
            state.load_snapshot("XCME", state.VenueHead(10, path, sha, rows))

    def test_a_missing_snapshot_is_refused(self, tree):
        with pytest.raises(state.StateCorrupt, match="does not exist"):
            state.load_snapshot("XCME", state.VenueHead(10, "XCME/gone.alloc.parquet", "x", 0))


class TestGlobalCounter:
    """The counter floor. Either the head or the scan alone must be enough."""

    def test_nothing_anywhere_is_zero(self, tree):
        assert state.global_counter(None) == (0, "")

    def test_the_head_alone_suffices(self, tree):
        """Every day directory lost: the head still knows how far numbering got."""
        assert state.global_counter(state.Head(counter=5_000)) == (5_000, state.HEAD_NAME)

    def test_the_scan_alone_suffices(self, tree):
        """The head lost: every day's sequence file still does."""
        counter_token.write_sequence("20260925", counter_token.Sequence(138_744))
        assert state.global_counter(None) == (138_744, "20260925")

    def test_whichever_is_higher_wins(self, tree):
        counter_token.write_sequence("20260925", counter_token.Sequence(138_744))
        assert state.global_counter(state.Head(counter=100))[0] == 138_744
        assert state.global_counter(state.Head(counter=200_000))[0] == 200_000


class TestVenueDays:
    def test_it_lists_only_days_that_hold_the_venue(self, tree):
        for day, mic in (("20260924", "XCME"), ("20260925", "XNSE"), ("20260926", "XCME")):
            counter_token.write_venue_manifest(
                day, mic, counter_token.VenueTokens(12, {"A": 1}, []))
        assert state.venue_days("xcme") == ["20260924", "20260926"]

    def test_a_days_allocation_reads_back_for_numbering(self, tree):
        counter_token.write_venue_manifest(
            "20260924", "XCME", counter_token.VenueTokens(12, {"A": 1, "B": 2}, [5]))
        alloc = state.day_alloc("20260924", "XCME")
        assert alloc.assigned == {"A": 1, "B": 2} and alloc.free == [5]
        assert state.day_alloc("20260925", "XCME") is None
