"""Data lineage: prove each stage's output came from the stage before it.

tokens.py asks whether counterTokenV2 is internally consistent. This asks a
different question -- did a row survive the chain unchanged, and can every row
at the end be traced to one at the beginning:

    raw DBN definition file
        -> deduped on instrument_id, mapped, rows with no symbol dropped
    normalized parquet   (paths.NORMALIZED_COLUMNS)
        -> expired contracts stripped, plugin schema applied
    plugin parquet       (docs/plugin/pg_data_types.txt)
        -> COPY-upserted on (token, trade_date)

Three failures this catches that nothing else does.

A definition file in the wrong day's directory. The normalizer only checks a
file's dataset and schema, not its date, so a Tuesday file dropped in
Wednesday's folder normalizes cleanly and produces a Wednesday output holding
Tuesday's instruments. Found live: 20260826/XNAS held equs-mini-20260825, and
20260826/XCME held both the 25th's and the 26th's files, silently merged.

A row that appears from nowhere. Every normalized scriptToken must be an
instrument_id the raw file actually carried.

A count that does not reconcile. Rows are dropped at two points on purpose --
duplicate instrument_ids, and rows the venue gave no symbol -- and stripped at a
third, for expiry. Each drop is counted and has to add up. "Fewer rows than
yesterday" is not a finding anyone can act on; "1,412 dropped for no symbol,
of which none were expected" is.

Reading the raw file costs ~1.7s for OPRA's 2M records, so this is a check you
run per day, not per push.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from .. import config, paths
from ..normalize import counter_token
from ..plugin import build as plugin_build
from .report import ALL, V2, Check, report


def _read(path: Path, columns: Sequence[str]):
    """Read only the columns a file actually has, and say which it lacks.

    A normalized file written before a column existed is a real thing to find --
    20260826's XCME output predated a column later added -- but it is a finding to
    report, not a reason for the tool to die. Anything keyed positionally on
    paths.NORMALIZED_COLUMNS would read that file shifted by one.
    """
    import pyarrow.parquet as pq

    present = set(pq.ParquetFile(path).schema_arrow.names)
    missing = [c for c in columns if c not in present]
    return pq.read_table(path, columns=[c for c in columns if c in present]), missing


def _raw_files(date_dir: str, mic: str) -> List[Path]:
    """The venue's dropped definition files, in the order the normalizer reads them."""
    directory = paths.manual_venue_dir(date_dir, mic)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.name.endswith((".dbn", ".dbn.zst")))


def _day_bounds(date_dir: str) -> tuple:
    start = datetime.strptime(date_dir, "%Y%m%d").replace(tzinfo=timezone.utc)
    return int(start.timestamp() * 1e9), int(start.timestamp() * 1e9) + 86_400_000_000_000


def _scan_raw(files: Sequence[Path], dataset: str) -> dict:
    """Read the raw drop exactly the way _manual_dbn_row_batches does.

    Same order, same dataset/schema skip, same first-file-wins dedupe on
    instrument_id -- if this diverged, the reconciliation below would be
    measuring a pipeline that does not exist.
    """
    import databento as db

    scan = {"files": [], "records": 0, "duplicates": 0, "blank_symbol": 0, "symbols": {}}
    seen = scan["symbols"]
    for path in files:
        store = db.DBNStore.from_file(path)
        meta = {
            "name": path.name,
            "dataset": str(store.dataset),
            "schema": str(store.schema),
            "start": store.metadata.start,
            "end": store.metadata.end,
            "records": 0,
        }
        scan["files"].append(meta)
        if meta["schema"] != "definition" or meta["dataset"] != dataset:
            meta["skipped"] = True
            continue
        for rec in store:
            if not isinstance(rec, db.InstrumentDefMsg):
                continue
            scan["records"] += 1
            meta["records"] += 1
            if rec.instrument_id in seen:
                scan["duplicates"] += 1
                continue
            symbol = rec.raw_symbol or ""
            if not symbol:
                scan["blank_symbol"] += 1
            seen[rec.instrument_id] = symbol
    return scan


def check_raw(date_dir: str, mic: str, files: Sequence[Path], scan: dict, dataset: str) -> List[Check]:
    """The drop itself, before anything reads it."""
    checks = []
    day_start, day_end = _day_bounds(date_dir)

    misplaced = [f for f in scan["files"]
                 if not (day_start <= f["start"] < day_end)]
    checks.append(Check(
        date_dir, mic, "raw is this day", not misplaced,
        f"{len(files)} file(s); " + (
            "; ".join(f"{f['name']} starts "
                      f"{datetime.fromtimestamp(f['start'] / 1e9, timezone.utc):%Y-%m-%d}"
                      for f in misplaced) + " -- normalized as this day's data"
            if misplaced else "every window starts inside the day"),
    ))

    wrong = [f for f in scan["files"]
             if f["dataset"] != dataset or f["schema"] != "definition"]
    checks.append(Check(
        date_dir, mic, "raw is this venue", not wrong,
        "; ".join(f"{f['name']} is {f['dataset']}/{f['schema']}, expected "
                  f"{dataset}/definition (skipped, so its rows are missing)"
                  for f in wrong) or f"{dataset}/definition",
    ))

    # More than one file is legal -- the reader stacks them -- but the result is
    # a blend of sessions under one date, which is never what was intended.
    checks.append(Check(
        date_dir, mic, "one session", len(scan["files"]) <= 1,
        ", ".join(f"{f['name']} ({f['records']:,})" for f in scan["files"])
        + (" -- merged into one day's output" if len(scan["files"]) > 1 else ""),
        hard=False,
    ))

    clamped = [f for f in scan["files"] if f["end"] and f["end"] < day_end]
    checks.append(Check(
        date_dir, mic, "full day", not clamped,
        "; ".join(f"{f['name']} ends "
                  f"{datetime.fromtimestamp(f['end'] / 1e9, timezone.utc):%H:%MZ}"
                  for f in clamped)
        + " (download clamped to what had published)" if clamped else "00:00Z..24:00Z",
        hard=False,
    ))
    return checks


def check_normalized(date_dir: str, mic: str, path: Path, scan: dict) -> List[Check]:
    """raw -> normalized: nothing invented, and every drop accounted for."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table, missing = _read(path, ["script", "scriptToken", "counterTokenV2"])
    rows = table.num_rows
    kept = len(scan["symbols"]) - scan["blank_symbol"]
    checks = [Check(
        date_dir, mic, "row accounting", rows == kept,
        f"{scan['records']:,} raw record(s) - {scan['duplicates']:,} duplicate id(s) "
        f"- {scan['blank_symbol']:,} with no symbol = {kept:,}, file has {rows:,}",
    )]

    # Every row must trace to an instrument the raw file carried. A normalized
    # row with no raw ancestor is a row this pipeline invented.
    raw_ids = {str(i) for i in scan["symbols"]}
    tokens = pc.unique(table.column("scriptToken")).to_pylist()
    orphans = [t for t in tokens if t not in raw_ids]
    checks.append(Check(
        date_dir, mic, "no invented rows", not orphans,
        f"{len(tokens):,} distinct scriptToken(s), {len(orphans):,} with no "
        f"matching instrument_id in the raw file"
        + (f" (e.g. {', '.join(orphans[:3])})" if orphans else ""),
    ))

    # The symbol has to survive the mapper untouched, or the token allocated
    # against it names something else.
    mismatched = sum(
        1 for token, script in zip(table.column("scriptToken").to_pylist(),
                                   table.column("script").to_pylist())
        if scan["symbols"].get(_as_id(token), script) != script)
    checks.append(Check(
        date_dir, mic, "symbol carried", mismatched == 0,
        f"{mismatched:,} row(s) whose script differs from the raw_symbol on the "
        f"same instrument_id",
    ))

    if missing:
        checks.append(Check(
            date_dir, mic, "schema current", False,
            f"written without {', '.join(missing)} -- a positional reader of "
            f"paths.NORMALIZED_COLUMNS would read this file shifted",
            hard=False))
    return checks


def _as_id(token: str):
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def check_plugin(date_dir: str, mic: str, normalized: Path, plugin: Path) -> List[Check]:
    """normalized -> plugin: the strip is the only difference, and the token holds."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    checks = []
    src, _ = _read(normalized, ["script", "counterTokenV2",
                                "def_expiration", "expiration"])
    # The real strip predicate, called rather than reimplemented -- a lineage
    # check that reimplements the rule it is checking cannot detect the rule
    # changing underneath it.
    cutoff = plugin_build._cutoff_ns(date_dir)
    expired = sum(1 for d, e in zip(src.column("def_expiration").to_pylist(),
                                    src.column("expiration").to_pylist())
                  if plugin_build._is_expired({"def_expiration": d, "expiration": e}, cutoff))

    out = pq.read_table(plugin)
    checks.append(Check(
        date_dir, mic, "strip accounting", src.num_rows - expired == out.num_rows,
        f"{src.num_rows:,} normalized - {expired:,} expired before "
        f"{date_dir} 00:00Z = {src.num_rows - expired:,}, plugin has {out.num_rows:,}",
    ))

    # name is the script and token is counterTokenV2, so the pair is the join
    # that has to survive the schema change.
    carried = dict(zip(src.column("script").to_pylist(),
                       src.column("counterTokenV2").to_pylist()))
    broken = sum(1 for name, token in zip(out.column("name").to_pylist(),
                                          out.column("token").to_pylist())
                 if carried.get(name) != token)
    checks.append(Check(
        date_dir, mic, "token carried", broken == 0,
        f"{broken:,} plugin row(s) whose token is not the counterTokenV2 of the "
        f"normalized row with the same script", tag=V2,
    ))

    empty = {c: n for c in out.schema.names
             if (n := (pc.sum(pc.or_(pc.is_null(out.column(c)),
                                     pc.equal(pc.cast(out.column(c), "string"), "")))
                       .as_py() or 0))}
    checks.append(Check(
        date_dir, mic, "no empty column", not empty,
        ", ".join(f"{c}={n:,}" for c, n in empty.items())
        or f"{len(out.schema.names)} column(s), none empty on {out.num_rows:,} rows",
    ))
    return checks


def check_recorded(date_dir: str, mic: str) -> List[Check]:
    """Hold the manifest's own record of what it read and wrote against disk.

    The other checks here re-derive lineage by inspecting files. This one checks
    the CLAIM: v4 headers name their inputs and outputs with a sha256, and a
    recorded digest that no longer matches means the file changed after the
    manifest was written. That is the difference between "these files look
    consistent today" and "these are the files this venue-day actually produced",
    which is the question that matters once the artefacts leave this machine.

    Silent when a header records nothing -- manifests migrated from v3 have no
    inputs or outputs, and reporting a missing key as a failure would drown the
    real ones.
    """
    import pyarrow.parquet as pq

    run = counter_token.venue_run(date_dir, mic)
    recorded = (run.get("inputs") or []) + (run.get("outputs") or [])
    if not recorded:
        return []

    base = paths.data_root() / date_dir
    missing, changed = [], []
    for item in recorded:
        path = base / str(item.get("path", ""))
        if not path.exists():
            missing.append(item.get("path", "?"))
            continue
        if counter_token.sha256_of(path) != item.get("sha256"):
            changed.append(item.get("path", "?"))

    checks = [Check(
        date_dir, mic, "recorded on disk", not missing,
        "; ".join(f"{name} is named by the manifest but absent" for name in missing)
        or f"{len(recorded)} file(s) named by the manifest are present",
    ), Check(
        date_dir, mic, "recorded unchanged", not changed,
        "; ".join(f"{name} no longer matches its recorded sha256" for name in changed)
        or f"{len(recorded) - len(missing)} file(s) still hash to what was recorded",
    )]

    rows = {str(o.get("path")): o.get("rows") for o in (run.get("outputs") or [])
            if o.get("rows")}
    if rows:
        wrong = []
        for name, claimed in rows.items():
            path = base / name
            if not path.exists():
                continue
            try:
                actual = pq.ParquetFile(path).metadata.num_rows
            except Exception as exc:
                wrong.append(f"{name} unreadable ({exc})")
                continue
            if actual != claimed:
                wrong.append(f"{name} holds {actual:,}, manifest says {claimed:,}")
        checks.append(Check(
            date_dir, mic, "recorded rows", not wrong,
            "; ".join(wrong) or
            f"{sum(rows.values()):,} row(s) recorded and counted",
        ))
    return checks


def check_day(date_dir: str, venues: Sequence[str] = ()) -> List[Check]:
    """Every stage, for one date directory."""
    wanted = {v.upper() for v in venues}
    exchanges = {cfg.venue_name.upper(): cfg for cfg in config.load_exchanges().values()}
    normalized_dir = paths.normalized_dir(date_dir)
    plugin_dir = paths.plugin_dir(date_dir)

    checks: List[Check] = []
    normalized_seen: Dict[str, Path] = {}
    for mic, cfg in sorted(exchanges.items()):
        if wanted and mic not in wanted:
            continue
        matches = sorted(normalized_dir.glob(f"{mic}-*.parquet")) if normalized_dir.is_dir() else []
        if not matches:
            continue
        normalized = matches[0]
        normalized_seen[mic] = normalized

        raw = _raw_files(date_dir, mic)
        if raw:
            scan = _scan_raw(raw, cfg.dataset)
            checks.extend(check_raw(date_dir, mic, raw, scan, cfg.dataset))
            checks.extend(check_normalized(date_dir, mic, normalized, scan))
        else:
            # Fyers venues and streamed-CSV days have no DBN drop to trace back
            # to. Saying so beats a silent gap in the report.
            checks.append(Check(
                date_dir, mic, "raw present", True,
                "no DBN drop -- normalized from a streamed source, lineage starts here",
                hard=False))

        built = plugin_dir / normalized.name
        if built.exists():
            checks.extend(check_plugin(date_dir, mic, normalized, built))

        checks.extend(check_recorded(date_dir, mic))

    # The pg key is (token, trade_date) with no venue column, so uniqueness has
    # to hold across every venue pushed for the day, not within each file.
    if plugin_dir.is_dir():
        checks.extend(_check_plugin_key(date_dir, plugin_dir, wanted))
    return checks


def _check_plugin_key(date_dir: str, plugin_dir: Path, wanted) -> List[Check]:
    import pyarrow.parquet as pq

    tokens, rows = set(), 0
    files = [p for p in sorted(plugin_dir.glob("*.parquet"))
             if not wanted or p.name.split("-")[0].upper() in wanted]
    if not files:
        return []
    for path in files:
        column = pq.read_table(path, columns=["token"]).column("token").to_pylist()
        rows += len(column)
        tokens |= set(column)
    return [Check(
        date_dir, "*", "pg key unique", len(tokens) == rows,
        f"{rows:,} row(s) across {len(files)} venue file(s), {len(tokens):,} distinct "
        f"token(s) -- (token, trade_date) is the primary key the push upserts on",
        tag=V2,
    )]


def run(dates: Sequence[str], venues: Sequence[str] = ()) -> int:
    checks: List[Check] = []
    for day in sorted(set(dates)):
        checks.extend(check_day(day, venues))
    return report(checks, suite="check-lineage")
