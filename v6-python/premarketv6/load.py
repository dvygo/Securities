"""The load stage: push a normalized day to its destinations through named sinks.

The pipeline is three stages. `download` fetches each vendor's masters,
`normalize` turns them into the canonical day (files only -- it never touches a
database), and `load` delivers that day to whoever consumes it:

    premarketv6 load --date-dir 20260925 --sink clickhouse-normal --sink postgres-plugin-xcme

A sink is a destination plus the shape it wants, described by one file,
conf/sinks/<name>.ini (gitignored -- it holds credentials; the .example files
beside it are the templates):

    [sink]
    type = clickhouse-normal | postgres-plugin | mdf-tokenmap
    ...                      # the type's own settings, below

  clickhouse-normal  the canonical schema, every venue: dated tables plus the
                     always-current `contracts`/`baskets` mirrors
  postgres-plugin    one market, projected onto the legacy 17-column symbol
                     master and upserted on (token, trade_date). Single-market
                     on purpose: the sink names its `market` and `venue_id`, and
                     the venue_id must match config.ini's, so a sink can never
                     push one venue's rows under another's identity
  mdf-tokenmap       the MDF lane's instrument_id -> counterTokenV2 map, as files,
                     into the day's own tree or MDF's delivery directory

Adding a destination of an existing type is a new sink file, not new code.
(`postgres-normal` -- the canonical schema into Postgres -- is planned, not built.)

Any setting can be overridden per run with PREMARKET_SINK_<NAME>_<KEY>, name
upper-cased with '-' as '_' (PREMARKET_SINK_CLICKHOUSE_NORMAL_PASSWORD).

Old data never reaches a live destination. A day older than the newest day
numbered live loads its dated ClickHouse tables but never the current mirrors,
and is refused outright for MDF's delivery directory. Every load is recorded in
the state run log with the files it read and what it wrote.
"""
import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

from . import config, export, paths
from .normalize import counter_token, state

CLICKHOUSE_NORMAL = "clickhouse-normal"
POSTGRES_PLUGIN = "postgres-plugin"
MDF_TOKENMAP = "mdf-tokenmap"
TYPES = (CLICKHOUSE_NORMAL, POSTGRES_PLUGIN, MDF_TOKENMAP)
PLANNED = {"postgres-normal": "the canonical schema into Postgres is planned, not built"}


class SinkError(ValueError):
    """A sink is misconfigured, or its load is refused. Nothing was written."""


@dataclass
class Sink:
    name: str
    type: str
    settings: Dict[str, str] = field(default_factory=dict)
    path: Path = Path()

    def get(self, key: str, default: str = "") -> str:
        env = os.getenv(f"PREMARKET_SINK_{self.name.upper().replace('-', '_')}_{key.upper()}")
        return env if env is not None else self.settings.get(key, default)

    def require(self, key: str) -> str:
        value = self.get(key)
        if not value:
            raise SinkError(f"sink {self.name}: `{key}` is not set in {self.path}")
        return value


def available() -> List[str]:
    directory = paths.sinks_dir()
    return sorted(p.stem for p in directory.glob("*.ini")) if directory.is_dir() else []


def load_sink(name: str) -> Sink:
    """Read and validate conf/sinks/<name>.ini. Raises SinkError, naming what exists."""
    path = paths.sinks_dir() / f"{name}.ini"
    if not path.exists():
        have = ", ".join(available()) or "none"
        raise SinkError(f"no sink named {name!r}: {path} does not exist "
                        f"(configured: {have}; templates: {paths.sinks_dir()}/*.ini.example)")
    parser = configparser.ConfigParser()
    parser.read(path)
    if "sink" not in parser:
        raise SinkError(f"{path} has no [sink] section")
    sink = Sink(name, parser["sink"].get("type", "").strip(), dict(parser["sink"]), path)
    if sink.type in PLANNED:
        raise SinkError(f"sink {name}: type {sink.type} -- {PLANNED[sink.type]}")
    if sink.type not in TYPES:
        raise SinkError(f"sink {name}: unknown type {sink.type!r} (one of {', '.join(TYPES)})")
    if sink.type == POSTGRES_PLUGIN:
        _plugin_market(sink)
    return sink


# -- what the day is, and how live it is -------------------------------------------------

def _newest_live(head: state.Head) -> str:
    return max((v.live_date for v in head.venues.values() if v.live_date), default="")


def _normalized_inputs(date_dir: str, markets=None) -> List[dict]:
    """The normalized files a sink reads, with the digests the run log records."""
    wanted = {m.upper() for m in markets} if markets else None
    out = []
    for path in export.normalized_files(date_dir):
        if wanted is None or path.name.split("-", 1)[0].upper() in wanted:
            out.append(counter_token.artifact(path, date_dir).as_dict())
    return out


# -- the adapters --------------------------------------------------------------------------

def _clickhouse_normal(date_dir: str, sink: Sink, head: state.Head, dry_run: bool,
                       log: Callable[[str], None]) -> dict:
    from . import clickhouse_export
    cfg = config.ClickHouseCfg(
        host=sink.require("host"), port=int(sink.get("port", "8123")),
        tcp_port=int(sink.get("tcp_port", "9000")), database=sink.require("database"),
        username=sink.get("username", "default"), password=sink.get("password", ""),
        secure=sink.get("secure", "false").strip().lower() in ("1", "true", "yes", "on"))
    newest = _newest_live(head)
    current = not newest or date_dir >= newest
    if not current:
        log(f"    {date_dir} is older than the newest live day {newest}: dated tables only, "
            f"the current contracts/baskets mirrors are left alone")
    inputs = _normalized_inputs(date_dir)
    if dry_run:
        log(f"    DRY RUN: would push {len(inputs)} normalized file(s) to "
            f"{cfg.host}:{cfg.port}/{cfg.database}"
            + ("" if current else " (dated tables only)"))
        return {"inputs": inputs, "outputs": []}
    result = clickhouse_export.push(cfg, date_dir, update_current=current)
    return {"inputs": inputs,
            "outputs": [{"path": f"clickhouse://{cfg.host}/{t}", "sha256": "",
                         "rows": result["contracts"] if "contracts" in t else result["baskets"]}
                        for t in result["tables"]],
            "current_updated": current}


def _plugin_market(sink: Sink):
    """A postgres-plugin sink's market and venue_id, checked against config.ini."""
    market = sink.require("market").strip().upper()
    if "," in market:
        raise SinkError(f"sink {sink.name}: a postgres-plugin sink pushes ONE market; "
                        f"make one sink file per market")
    venue_id = int(sink.require("venue_id"))
    configured = config.load_exchanges().get(market.lower())
    if configured is None:
        raise SinkError(f"sink {sink.name}: market {market} is not in config.ini")
    if configured.venue_id != venue_id:
        raise SinkError(f"sink {sink.name}: venue_id {venue_id} but config.ini numbers "
                        f"{market} as venue_id {configured.venue_id} -- refusing to push "
                        f"one venue's rows under another's identity")
    if not configured.enabled:
        raise SinkError(f"sink {sink.name}: {market} is disabled (enabled = 0)")
    return market, venue_id


def _postgres_plugin(date_dir: str, sink: Sink, head: state.Head, dry_run: bool,
                     log: Callable[[str], None]) -> dict:
    from .plugin import build, postgres
    market, _ = _plugin_market(sink)
    cfg = config.PostgresPluginCfg(
        database_url=sink.require("database_url"), schema=sink.require("schema"),
        table=sink.require("table"), exchanges=[market],
        create_table=sink.get("create_table", "0").strip() == "1")
    inputs = _normalized_inputs(date_dir, [market])
    if dry_run:
        log(f"    DRY RUN: would build the plugin file for {market} from "
            f"{len(inputs)} normalized file(s) and upsert it into {cfg.schema}.{cfg.table}")
        return {"inputs": inputs, "outputs": []}
    files = build.build_day(date_dir, [market])
    results = postgres.push(cfg, files)
    return {"inputs": inputs,
            "outputs": [{"path": f"postgres://{cfg.schema}.{cfg.table}#{r['file'].name}",
                         "sha256": counter_token.sha256_of(r["file"]), "rows": r["upserted"]}
                        for r in results]}


def _mdf_tokenmap(date_dir: str, sink: Sink, head: state.Head, dry_run: bool,
                  log: Callable[[str], None]) -> dict:
    from .plugin import tokenmap
    exchanges = config.load_exchanges()
    listed = [m.strip().upper() for m in sink.get("markets", "").split(",") if m.strip()]
    markets = listed or sorted(c.venue_name.upper() for c in exchanges.values()
                               if c.enabled and c.feed == "databento")
    delivery = sink.get("dir").strip()
    out_dir = Path(delivery) if delivery else tokenmap.tokenmap_dir(date_dir)
    if delivery:
        # MDF loads whatever map is in its directory; the file name carries no
        # date. An older day's map would silently replace the live one.
        stale = [m for m in markets
                 if (v := head.venues.get(m)) is not None and v.live_date
                 and date_dir < v.live_date]
        if stale:
            raise SinkError(f"sink {sink.name}: refusing to deliver {date_dir}'s map for "
                            f"{', '.join(stale)} into {out_dir} -- the newest live day is "
                            f"newer, and MDF would load the old map as current. Load it with "
                            f"a sink that writes into the day's own tree (no `dir`).")
    inputs = _normalized_inputs(date_dir, markets)
    if dry_run:
        log(f"    DRY RUN: would write token maps for {', '.join(markets)} into {out_dir}")
        return {"inputs": inputs, "outputs": []}
    written = tokenmap.emit(date_dir, markets, out_dir)
    return {"inputs": inputs,
            "outputs": [{"path": str(p), "sha256": counter_token.sha256_of(p), "rows": 0}
                        for p in written]}


ADAPTERS = {CLICKHOUSE_NORMAL: _clickhouse_normal, POSTGRES_PLUGIN: _postgres_plugin,
            MDF_TOKENMAP: _mdf_tokenmap}


# -- the command -----------------------------------------------------------------------------

def run(date_dir: str, names: List[str], dry_run: bool = False,
        log: Callable[[str], None] = print) -> int:
    """Load one day into every named sink. Non-zero if any sink failed.

    Every sink is read and validated before anything is pushed, so a typo in the
    third sink cannot leave the first two loaded and the run half-done. Loads
    are serialised with numbering runs by the same state lock: a load never
    reads a day while a normalize is rewriting it.
    """
    if not names:
        raise SinkError(f"name at least one --sink (configured: {', '.join(available()) or 'none'})")
    sinks = [load_sink(name) for name in dict.fromkeys(names)]
    failures: List[str] = []
    with state.lock("load"):
        head = state.require_head()
        run_id = state.new_run_id(date_dir, "load")
        if not dry_run:
            state.append_event(state.event(
                "START", run_id, "load",
                {"date": date_dir, "sinks": [s.name for s in sinks],
                 "operator": state.operator()}))
        for sink in sinks:
            log(f">>> load {sink.name} ({sink.type})")
            child = state.new_run_id(date_dir, "load")
            try:
                result = ADAPTERS[sink.type](date_dir, sink, head, dry_run, log)
            except Exception as exc:                        # noqa: BLE001 - reported
                failures.append(f"{sink.name}: {exc}")
                log(f"error: load {sink.name}: {exc}")
                if not dry_run:
                    state.append_event(state.event(
                        "FAIL", child, "load", {"sink": sink.name, "type": sink.type,
                                                "date": date_dir, "error": str(exc)},
                        parent=run_id))
                continue
            if not dry_run:
                facets = {"sink": sink.name, "type": sink.type, "date": date_dir}
                if "current_updated" in result:
                    facets["current_updated"] = result["current_updated"]
                state.append_event(state.event(
                    "COMPLETE", child, "load", facets, inputs=result["inputs"],
                    outputs=result["outputs"], parent=run_id))
        if not dry_run:
            state.append_event(state.event(
                "FAIL" if failures else "COMPLETE", run_id, "load",
                {"date": date_dir, "failures": failures}))
    return 1 if failures else 0
