"""The load stage (premarketv6/load.py): named sinks, their validation, the
old-day interlocks, and the run log -- with every destination faked, so no
database or MDF host is ever touched."""
import pytest

from premarketv6 import load, paths
from premarketv6.normalize import counter_token, state

CONFIG = """[paths]
data_dir = {root}

[EXCHANGE:XCME]
feed = databento
enabled = 1
venue_id = 12
dataset = GLBX.MDP3

[EXCHANGE:XNAS]
feed = databento
enabled = 1
venue_id = 14
dataset = EQUS.MINI

[EXCHANGE:XCBO]
feed = databento
enabled = 0
venue_id = 10
dataset = OPRA.PILLAR
"""

LIVE = "20260926"          # the newest day numbered live
OLDER = "20260925"         # a day older than it


def _normalized(day, mic):
    import pyarrow as pa
    import pyarrow.parquet as pq
    directory = paths.normalized_dir(day)
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"script": ["S"], "counterTokenV2": ["1"]}),
                   directory / f"{mic}-DATABENTO-normalized.parquet")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("PREMARKET_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PREMARKET_SINKS", str(tmp_path / "sinks"))
    (tmp_path / "sinks").mkdir()
    conf = tmp_path / "config.ini"
    conf.write_text(CONFIG.format(root=tmp_path / "data"))
    monkeypatch.setattr(paths, "config_ini", lambda: conf)
    counter_token._exchanges.cache_clear()
    counter_token.write_sequence(LIVE, counter_token.Sequence(2))
    counter_token.write_venue_manifest(LIVE, "XCME", counter_token.VenueTokens(12, {"A": 1}, []))
    counter_token.write_venue_manifest(LIVE, "XNAS", counter_token.VenueTokens(14, {"B": 2}, []))
    for day in (OLDER, LIVE):
        for mic in ("XCME", "XNAS"):
            _normalized(day, mic)
    state.init_state("test", log=lambda *_: None)
    yield tmp_path
    counter_token._exchanges.cache_clear()


def _sink(tree, name, **settings):
    body = "[sink]\n" + "".join(f"{k} = {v}\n" for k, v in settings.items())
    (tree / "sinks" / f"{name}.ini").write_text(body)


def _quiet(*_):
    pass


class TestSinkConfig:
    def test_a_missing_sink_names_what_exists(self, tree):
        _sink(tree, "clickhouse-normal", type="clickhouse-normal")
        with pytest.raises(load.SinkError, match="configured: clickhouse-normal"):
            load.load_sink("clickhose-normal")

    def test_an_unknown_type_is_refused(self, tree):
        _sink(tree, "x", type="kafka")
        with pytest.raises(load.SinkError, match="unknown type"):
            load.load_sink("x")

    def test_postgres_normal_is_planned_not_built(self, tree):
        _sink(tree, "pg", type="postgres-normal")
        with pytest.raises(load.SinkError, match="planned"):
            load.load_sink("pg")

    def test_a_plugin_sink_pushes_one_market(self, tree):
        _sink(tree, "p", type="postgres-plugin", market="XCME,XNAS", venue_id=12)
        with pytest.raises(load.SinkError, match="ONE market"):
            load.load_sink("p")

    def test_a_plugin_sinks_venue_id_must_match_config(self, tree):
        _sink(tree, "p", type="postgres-plugin", market="XNAS", venue_id=12)
        with pytest.raises(load.SinkError, match="another's identity"):
            load.load_sink("p")

    def test_a_disabled_market_is_refused(self, tree):
        _sink(tree, "p", type="postgres-plugin", market="XCBO", venue_id=10)
        with pytest.raises(load.SinkError, match="disabled"):
            load.load_sink("p")

    def test_an_env_var_overrides_a_setting(self, tree, monkeypatch):
        _sink(tree, "clickhouse-normal", type="clickhouse-normal", password="from-file")
        monkeypatch.setenv("PREMARKET_SINK_CLICKHOUSE_NORMAL_PASSWORD", "from-env")
        assert load.load_sink("clickhouse-normal").get("password") == "from-env"


class TestLoadRun:
    def test_every_sink_is_validated_before_anything_is_pushed(self, tree, monkeypatch):
        called = []
        monkeypatch.setitem(load.ADAPTERS, "clickhouse-normal",
                            lambda *a, **k: called.append(1) or {"inputs": [], "outputs": []})
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        with pytest.raises(load.SinkError):
            load.run(LIVE, ["ch", "missing"], log=_quiet)
        assert called == []

    def test_the_run_log_records_each_sink(self, tree, monkeypatch):
        monkeypatch.setitem(load.ADAPTERS, "clickhouse-normal", lambda *a, **k: {
            "inputs": [{"path": "x.parquet", "sha256": "ab", "rows": 1}],
            "outputs": [{"path": "clickhouse://h/d.contracts", "sha256": "", "rows": 1}]})
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        assert load.run(LIVE, ["ch"], log=_quiet) == 0
        loads = [e for e in state.read_events() if e["job"]["name"] == "load"]
        assert [e["eventType"] for e in loads] == ["START", "COMPLETE", "COMPLETE"]
        child = loads[1]
        assert child["run"]["facets"]["premarketv6"]["sink"] == "ch"
        assert child["inputs"][0]["name"] == "x.parquet"
        assert child["run"]["facets"]["parent"]["run"]["runId"] == loads[0]["run"]["runId"]

    def test_a_failing_sink_fails_the_load_but_the_others_still_run(self, tree, monkeypatch):
        ran = []

        def boom(*a, **k):
            raise RuntimeError("server down")

        monkeypatch.setitem(load.ADAPTERS, "clickhouse-normal", boom)
        monkeypatch.setitem(load.ADAPTERS, "mdf-tokenmap",
                            lambda *a, **k: ran.append(1) or {"inputs": [], "outputs": []})
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        _sink(tree, "mdf", type="mdf-tokenmap")
        assert load.run(LIVE, ["ch", "mdf"], log=_quiet) == 1
        assert ran == [1]
        kinds = [e["eventType"] for e in state.read_events() if e["job"]["name"] == "load"]
        assert kinds == ["START", "FAIL", "COMPLETE", "FAIL"]

    def test_a_dry_run_connects_to_nothing_and_logs_nothing(self, tree, monkeypatch):
        from premarketv6 import clickhouse_export
        monkeypatch.setattr(clickhouse_export, "push", lambda *a, **k: pytest.fail("pushed"))
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        before = len(state.read_events())
        assert load.run(LIVE, ["ch"], dry_run=True, log=_quiet) == 0
        assert len(state.read_events()) == before

    def test_load_needs_the_state(self, tree):
        state.head_path().unlink()
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        with pytest.raises(state.StateMissing):
            load.run(LIVE, ["ch"], log=_quiet)


class TestInterlocks:
    """An older day never becomes what a live reader takes as current."""

    @staticmethod
    def _capture_clickhouse(monkeypatch):
        from premarketv6 import clickhouse_export
        seen = {}

        def push(cfg, date_dir, update_current=True):
            seen[date_dir] = update_current
            return {"contracts": 1, "baskets": 0, "tables": ["d.contracts_x"]}

        monkeypatch.setattr(clickhouse_export, "push", push)
        return seen

    def test_the_clickhouse_current_mirror_moves_only_for_the_newest_live_day(
            self, tree, monkeypatch):
        seen = self._capture_clickhouse(monkeypatch)
        _sink(tree, "ch", type="clickhouse-normal", host="h", database="d")
        assert load.run(LIVE, ["ch"], log=_quiet) == 0
        assert load.run(OLDER, ["ch"], log=_quiet) == 0
        assert seen == {LIVE: True, OLDER: False}

    def test_delivering_an_older_days_token_map_to_mdf_is_refused(self, tree, monkeypatch):
        from premarketv6.plugin import tokenmap
        monkeypatch.setattr(tokenmap, "emit", lambda *a, **k: pytest.fail("delivered"))
        _sink(tree, "mdf", type="mdf-tokenmap", dir=str(tree / "cpp-vendor"))
        assert load.run(OLDER, ["mdf"], log=_quiet) == 1
        [failed] = [e for e in state.read_events()
                    if e["eventType"] == "FAIL" and "sink" in e["run"]["facets"]["premarketv6"]]
        assert "refusing to deliver" in failed["run"]["facets"]["premarketv6"]["error"]

    def test_an_older_days_token_map_may_go_into_its_own_tree(self, tree, monkeypatch):
        from premarketv6.plugin import tokenmap
        calls = []
        monkeypatch.setattr(tokenmap, "emit",
                            lambda date_dir, markets, out_dir: calls.append(
                                (date_dir, markets, out_dir)) or [])
        _sink(tree, "mdf", type="mdf-tokenmap")
        assert load.run(OLDER, ["mdf"], log=_quiet) == 0
        [(date_dir, markets, out_dir)] = calls
        assert out_dir == tokenmap.tokenmap_dir(OLDER)
        assert markets == ["XCME", "XNAS"]          # enabled Databento venues; XCBO is off

    def test_a_plugin_sink_builds_and_pushes_only_its_market(self, tree, monkeypatch):
        from premarketv6.plugin import build, postgres
        built, pushed = [], []
        monkeypatch.setattr(build, "build_day",
                            lambda date_dir, markets: built.append(markets) or [])
        monkeypatch.setattr(postgres, "push",
                            lambda cfg, files: pushed.append(cfg) or [])
        _sink(tree, "postgres-plugin-xnas", type="postgres-plugin", market="XNAS",
              venue_id=14, database_url="postgres://x", schema="public", table="resultset")
        assert load.run(LIVE, ["postgres-plugin-xnas"], log=_quiet) == 0
        assert built == [["XNAS"]]
        assert pushed[0].exchanges == ["XNAS"] and pushed[0].table == "resultset"


def test_every_shipped_template_is_a_valid_sink(tmp_path, monkeypatch):
    """conf/sinks/*.ini.example, copied to .ini as the runbook says, must load --
    against the example config, whose venue_ids the plugin templates repeat."""
    import shutil
    templates = sorted((paths.repo_root() / "conf" / "sinks").glob("*.ini.example"))
    assert {t.name.split(".")[0] for t in templates} >= {
        "clickhouse-normal", "mdf-tokenmap", "postgres-plugin-xcme",
        "postgres-plugin-xcbo", "postgres-plugin-xnas"}
    for template in templates:
        shutil.copy(template, tmp_path / template.name.replace(".ini.example", ".ini"))
    monkeypatch.setenv("PREMARKET_SINKS", str(tmp_path))
    monkeypatch.setattr(paths, "config_ini",
                        lambda: paths.repo_root() / "conf" / "config.ini.example")
    counter_token._exchanges.cache_clear()
    try:
        types = {load.load_sink(name).type for name in load.available()}
    finally:
        counter_token._exchanges.cache_clear()
    assert types == set(load.TYPES)
