"""counterTokenV2 data validation: the checks that run over what was written.

counter_token.py's unit tests pin the numbering RULES. These pin the OUTPUT --
the parquet a run actually wrote and the manifest it wrote beside it. The two
catch different things, and only the files can show the failures that matter
most: a venue whose manifest was written but whose parquet was aborted, or a
Wednesday backfilled after the Thursday that already chained past it.

Checks are hard or soft:

  hard  an invariant counterTokenV2 promises. A failure means a bug or a
        corrupt day, and the run exits non-zero.
  soft  a property counterTokenV2 does NOT promise, reported because the number
        is worth watching rather than because it is wrong. Offset reuse is the
        one that matters: when a script departs, its offset returns to the free
        pool and a later script takes it, so one token can name two different
        instruments on two dates. Measured on real OPRA data across the
        2026-08-26 gap: 9,687 such tokens. That is by design -- counterTokenV3
        is the column that does not do it -- so it is counted, not failed.

Deliberately no new dependency: pyarrow is already how this pipeline reads and
writes parquet, and the tool has to survive being frozen into the binary devops
runs.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config, paths
from . import counter_token


@dataclass
class Check:
    """One verdict. `hard` is False for a property v2 never promised."""
    day: str
    venue: str
    name: str
    ok: bool
    detail: str
    hard: bool = True

    @property
    def status(self) -> str:
        return "PASS" if self.ok else ("FAIL" if self.hard else "WARN")


def decode(prefix: int, token: str) -> Optional[int]:
    """The 1-based row `assign(prefix, n)` would have numbered `token`.

    The inverse of assign(): strip the two prefix digits, and the width of what
    is left says which block it came from. None when the token is not on this
    prefix at all, which is itself the finding.
    """
    head = str(prefix)
    if not token.isdigit() or not token.startswith(head) or len(token) <= len(head):
        return None
    rest = token[len(head):]
    return sum(10 ** w for w in range(1, len(rest))) + int(rest) + 1


def _venue_files(date_dir: str) -> Dict[str, List[Path]]:
    """MIC -> the normalized parquet(s) written for it that day."""
    found: Dict[str, List[Path]] = {}
    directory = paths.normalized_dir(date_dir)
    if not directory.exists():
        return found
    for path in sorted(directory.glob("*.parquet")):
        found.setdefault(path.name.split("-")[0].upper(), []).append(path)
    return found


def _configured() -> Dict[str, object]:
    """MIC -> ExchangeCfg, for every venue config.ini numbers."""
    return {cfg.venue_name.upper(): cfg for cfg in config.load_exchanges().values()}


def _column(table, name):
    """A column as a combined chunked array, or None when the file lacks it."""
    return table.column(name) if name in table.schema.names else None


def check_day(date_dir: str, venues: Sequence[str] = ()) -> List[Check]:
    """Every within-day invariant, for one date directory."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    wanted = {v.upper() for v in venues}
    files = _venue_files(date_dir)
    exchanges = _configured()
    manifest = counter_token.load_manifest(date_dir)
    entries = (manifest.get("venues") or {})

    checks: List[Check] = []
    seen_tokens: Dict[str, set] = {}

    # A manifest naming a venue with no parquet is the hazard that cost a day
    # during the v3 gap test: deleting a day's data does NOT delete it from the
    # chain, so the next run carries an allocation forward from output nobody
    # can point at any more.
    for mic in sorted(set(entries) - set(files)):
        if wanted and mic not in wanted:
            continue
        checks.append(Check(
            date_dir, mic, "manifest has data",
            ok=False,
            detail=f"manifest allocates {len(entries[mic].get('assigned') or {}):,} "
                   "script(s) but no normalized parquet exists -- the next day "
                   "will still carry this allocation forward",
        ))

    for mic in sorted(files):
        if wanted and mic not in wanted:
            continue
        cfg = exchanges.get(mic)
        if cfg is None or not cfg.venue_id:
            continue                       # opted out of numbering; nothing to check
        prefix = cfg.counter_prefix_v2

        table = pq.read_table(files[mic], columns=["script", "counterToken", "counterTokenV2"])
        rows = table.num_rows
        if not rows:
            continue
        v2 = table.column("counterTokenV2")

        blank = pc.sum(pc.or_(pc.is_null(v2), pc.equal(v2, ""))).as_py() or 0
        checks.append(Check(
            date_dir, mic, "populated", blank == 0,
            f"{rows:,} row(s), {blank:,} blank" + (
                " -- a blank token means the script was not in the day's "
                "allocation, so the manifest and the file disagree" if blank else ""),
        ))

        numeric = pc.match_substring_regex(v2, r"^[0-9]+$")
        bad = rows - (pc.sum(numeric).as_py() or 0)
        widest = 0
        if bad < rows:
            widest = pc.max(pc.cast(v2.filter(numeric), "int64")).as_py() or 0
        fits = bad == 0 and widest <= counter_token.INT32_MAX
        checks.append(Check(
            date_dir, mic, "numeric int32", fits,
            f"{bad:,} non-numeric, highest {widest:,} "
            f"(int32 {counter_token.INT32_MAX:,})",
        ))

        off_prefix = rows - (pc.sum(pc.starts_with(v2, str(prefix))).as_py() or 0)
        checks.append(Check(
            date_dir, mic, "prefix", off_prefix == 0,
            f"prefix {prefix} (venue_id {cfg.venue_id} + 1), {off_prefix:,} off it",
        ))

        # One-to-one both ways. A script with two tokens breaks any join on the
        # token; a token naming two scripts breaks the pg key (token, trade_date)
        # the plugin pushes to.
        pairs = table.select(["script", "counterTokenV2"]).group_by(
            ["script", "counterTokenV2"]).aggregate([])
        scripts = pc.count_distinct(pairs.column("script")).as_py()
        tokens = pc.count_distinct(pairs.column("counterTokenV2")).as_py()
        checks.append(Check(
            date_dir, mic, "one-to-one",
            pairs.num_rows == scripts == tokens,
            f"{pairs.num_rows:,} distinct pair(s), {scripts:,} script(s), "
            f"{tokens:,} token(s)",
        ))
        seen_tokens[mic] = set(pairs.column("counterTokenV2").to_pylist())

        # v1 and v2 are separate columns of one row and must stay separable as
        # integers -- that is the whole reason a venue owns venue_id AND
        # venue_id+1 rather than sharing one prefix.
        v1 = _column(table, "counterToken")
        overlap = 0
        if v1 is not None:
            distinct_v1 = pc.unique(v1)
            overlap = pc.sum(pc.is_in(pairs.column("counterTokenV2"),
                                      value_set=distinct_v1)).as_py() or 0
        checks.append(Check(
            date_dir, mic, "v1 disjoint", overlap == 0,
            f"{overlap:,} counterTokenV2 value(s) also appear as a counterToken",
        ))

        checks.extend(_check_manifest(date_dir, mic, cfg, prefix, pairs, entries.get(mic)))

    # Two venues sharing a token defeats the (token, trade_date) key just as
    # surely as a within-venue collision does.
    if len(seen_tokens) > 1:
        total = sum(len(t) for t in seen_tokens.values())
        union = len(set().union(*seen_tokens.values()))
        checks.append(Check(
            date_dir, "*", "venues disjoint", total == union,
            f"{total - union:,} token(s) shared between venues "
            f"({', '.join(sorted(seen_tokens))})",
        ))
    return checks


def _check_manifest(date_dir, mic, cfg, prefix, pairs, entry) -> List[Check]:
    """The parquet against the manifest written beside it.

    The manifest is what tomorrow reads. If it disagrees with today's file,
    tomorrow inherits the disagreement and nothing downstream can tell. `entry`
    is passed in rather than re-read: a day's manifest is 90MB on an OPRA week
    and every venue would otherwise reload it.
    """
    if entry is None:
        return [Check(date_dir, mic, "manifest present", False,
                      "a normalized parquet exists but the manifest does not "
                      "name this venue -- tomorrow will renumber from scratch")]

    assigned = {str(k): int(v) for k, v in (entry.get("assigned") or {}).items()}
    free = [int(x) for x in (entry.get("free") or [])]
    high_water = int(entry.get("high_water", 0))
    checks = []

    problems = []
    if int(entry.get("venue_id", 0)) != cfg.venue_id:
        problems.append(f"venue_id {entry.get('venue_id')} != config {cfg.venue_id}")
    if int(entry.get("prefix", 0)) != prefix:
        problems.append(f"prefix {entry.get('prefix')} != venue_id+1 {prefix}")
    if int(entry.get("count", -1)) != len(assigned):
        problems.append(f"count {entry.get('count')} != {len(assigned)} assigned")
    if assigned and high_water < max(assigned.values()):
        problems.append(f"high_water {high_water} below the highest offset "
                        f"{max(assigned.values())}")
    reissued = set(free) & set(assigned.values())
    if reissued:
        problems.append(f"{len(reissued):,} offset(s) in both free and assigned -- "
                        "the pool would hand out a live number")
    checks.append(Check(date_dir, mic, "manifest internal", not problems,
                        "; ".join(problems) or
                        f"high_water {high_water:,}, {len(assigned):,} assigned, "
                        f"{len(free):,} free"))

    # Every token in the file re-derived from the manifest offset. This is the
    # check that ties the two artefacts together; the rest only inspect one.
    missing = wrong = 0
    for script, token in zip(pairs.column("script").to_pylist(),
                             pairs.column("counterTokenV2").to_pylist()):
        offset = assigned.get(script)
        if offset is None:
            missing += 1
        elif counter_token.assign(prefix, offset) != token:
            wrong += 1
    orphans = len(assigned) - (pairs.num_rows - missing)
    checks.append(Check(
        date_dir, mic, "manifest agrees", missing == wrong == 0 and orphans == 0,
        f"{missing:,} script(s) in the file but not the manifest, "
        f"{wrong:,} token(s) that do not re-derive, "
        f"{orphans:,} allocated to script(s) the file does not carry",
    ))
    return checks


def check_pair(previous: str, current: str, venues: Sequence[str] = ()) -> List[Check]:
    """The carry-forward contract, between two dates.

    Both directions matter and they fail differently. A script that MOVED broke
    the chain -- the point of v2 is that it does not. A token that became
    AMBIGUOUS is v2 doing what v2 does, and is counted rather than failed.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    wanted = {v.upper() for v in venues}
    before, after = _venue_files(previous), _venue_files(current)
    checks: List[Check] = []

    for mic in sorted(set(before) & set(after)):
        if wanted and mic not in wanted:
            continue
        span = f"{previous} -> {current}"

        def distinct(files, suffix):
            table = pq.read_table(files, columns=["script", "counterTokenV2"])
            table = table.group_by(["script", "counterTokenV2"]).aggregate([])
            return table.rename_columns(["script", f"token_{suffix}"])

        old, new = distinct(before[mic], "a"), distinct(after[mic], "b")
        joined = old.join(new, keys="script", join_type="inner")
        shared = joined.num_rows
        moved = pc.sum(pc.not_equal(joined.column("token_a"),
                                    joined.column("token_b"))).as_py() or 0
        checks.append(Check(
            span, mic, "stable", moved == 0,
            f"{shared:,} script(s) on both days, {moved:,} moved to a different "
            "token" + (" -- the carry-forward chain is broken" if moved else ""),
        ))

        by_token = old.rename_columns(["script_a", "token"]).join(
            new.rename_columns(["script_b", "token"]), keys="token", join_type="inner")
        ambiguous = pc.sum(pc.not_equal(by_token.column("script_a"),
                                        by_token.column("script_b"))).as_py() or 0
        checks.append(Check(
            span, mic, "no reuse", ambiguous == 0,
            f"{ambiguous:,} token(s) name a different script on the two days"
            + (" -- v2 recycles a departed script's offset, so this is expected, "
               "not a regression; counterTokenV3 is the column that cannot do it"
               if ambiguous else ""),
            hard=False,
        ))

        # Which day v2 actually chained from. Across a gap it silently reaches
        # further back, and the allocation it inherits is older than the data.
        cfg = _configured().get(mic)
        if cfg is not None and cfg.venue_id:
            source, stamp = counter_token.previous_tokens(current, mic, cfg.venue_id)
            checks.append(Check(
                span, mic, "chained from", source is not None and stamp == previous,
                f"carried from {stamp or 'nothing -- renumbered from scratch'}"
                + ("" if stamp == previous else
                   f", not from {previous}; the days between were numbered "
                   "against an older allocation"),
            ))
    return checks


def report(checks: Sequence[Check]) -> int:
    """Print grouped by day and venue. Non-zero when a hard check failed."""
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


def run(dates: Sequence[str], venues: Sequence[str] = ()) -> int:
    """Validate each date, then each consecutive pair of them."""
    ordered = sorted(set(dates))
    checks: List[Check] = []
    for day in ordered:
        checks.extend(check_day(day, venues))
    for previous, current in zip(ordered, ordered[1:]):
        checks.extend(check_pair(previous, current, venues))
    return report(checks)
