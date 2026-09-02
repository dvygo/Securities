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
        2026-08-26 gap: 9,687 such tokens. That is what the design accepts in
        exchange for a token that stays inside int32, so it is counted, not
        failed. Anything joining on a token across dates has to carry the date.

Deliberately no new dependency: pyarrow is already how this pipeline reads and
writes parquet, and the tool has to survive being frozen into the binary devops
runs.
"""
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config, paths
from ..normalize import counter_token
from .report import ALL, V2, Check, report


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


def _entry(date_dir: str, mic: str):
    """A venue's allocation, and the corruption message if it could not be read.

    counter_token raises on a header whose allocation table is missing or does
    not hash, because for the normalizer the only safe response is to skip the
    venue rather than renumber it. For a QA run the right response is the
    opposite: report it as a failed check and keep going, so one bad venue does
    not hide the state of the other five.
    """
    try:
        return counter_token.venue_entry(date_dir, mic), ""
    except counter_token.ManifestCorrupt as exc:
        return {}, str(exc)


def _allocation(date_dir: str, mic: str):
    """One venue's allocation for a day, without holding the rest.

    The pair checks need two days in hand at once, so everything but the venue
    asked for is dropped as soon as it is extracted.
    """
    entry, _ = _entry(date_dir, mic)
    if not entry:
        return None
    return {
        "assigned": {str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
        "free": sorted(int(x) for x in (entry.get("free") or [])),
    }


def check_pair_recycling(previous: str, current: str, mic: str) -> List[Check]:
    """Did carry_forward's three rules actually fire between these two days?

    The other pair checks read the tokens in the files. These read the
    ALLOCATION that produced them, which is where reuse either happens or
    silently does not -- and no token-level check can tell the two apart. A
    venue that draws exactly its arrival count from the sequence every day looks
    identical to one that recycled nothing because it had nothing to recycle;
    only the pool arithmetic distinguishes them.

    Measured on the real week: XCBO 24->25 released 11,058 tokens and gave
    7,083 of them to arrivals without drawing from the sequence at all, while
    XCME had zero departures all week and so never exercised the path.
    """
    before, after = _allocation(previous, mic), _allocation(current, mic)
    span = f"{previous} -> {current}"
    if before is None or after is None:
        return [Check(span, mic, "recycling", False,
                      "one of the two days has no manifest entry for this venue",
                      hard=False, tag=V2)]

    kept = set(before["assigned"]) & set(after["assigned"])
    departed = set(before["assigned"]) - kept
    arrived = set(after["assigned"]) - kept

    released = sorted({before["assigned"][s] for s in departed})
    available = sorted(set(before["free"]) | set(released))
    available_set = set(available)
    taken = sorted(after["assigned"][s] for s in arrived
                   if after["assigned"][s] in available_set)
    extended = [s for s in arrived if after["assigned"][s] not in available_set]

    return [
        # Rule 2: a departed script's offset goes back in the pool.
        Check(span, mic, "offsets released",
              set(released).isdisjoint(after["assigned"].values()) or bool(taken),
              f"{len(departed):,} script(s) departed, {len(released):,} offset(s) "
              f"released; pool in {len(before['free']):,} -> {len(available):,} available",
              tag=V2),
        # Rule 3, and the one that actually matters: the venue's pool is drained
        # BEFORE a fresh number is drawn from the shared sequence. Drawing early
        # leaks numbers out of a space every venue now shares.
        Check(span, mic, "pool drained first",
              len(taken) == min(len(arrived), len(available)),
              f"{len(arrived):,} arrival(s): {len(taken):,} reused a released "
              f"token (available {len(available):,}), {len(extended):,} drew a "
              f"new one from the shared sequence",
              tag=V2),
        # Leftovers have to survive the day or the numbers are lost for good.
        Check(span, mic, "pool carried",
              after["free"] == available[len(taken):],
              f"{len(after['free']):,} offset(s) still free for tomorrow "
              f"(expected {len(available) - len(taken):,})",
              tag=V2),
    ]


def check_day(date_dir: str, venues: Sequence[str] = ()) -> List[Check]:
    """Every within-day invariant, for one date directory."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    wanted = {v.upper() for v in venues}
    files = _venue_files(date_dir)
    exchanges = _configured()
    entries: Dict[str, dict] = {}
    unreadable: Dict[str, str] = {}
    for mic in counter_token.venues_with_manifest(date_dir):
        entry, why = _entry(date_dir, mic)
        if why:
            unreadable[mic] = why
        elif entry:
            entries[mic] = entry

    checks: List[Check] = []
    # A header whose table will not load is worse than a missing venue: the
    # header still says the venue completed, so anything counting manifests
    # believes the day is done.
    for mic, why in sorted(unreadable.items()):
        checks.append(Check(date_dir, mic, "manifest readable", False, why))
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

        # There is deliberately no "v1 disjoint" check any more. counterToken is
        # positional 1..N within a venue-day and counterTokenV2 comes from the
        # shared sequence, so the two overlap by construction. They are told
        # apart by column, not by value -- which is why only v2 is ever joined
        # on, and only v2 is what the plugin pushes.

        checks.extend(_check_manifest(date_dir, mic, cfg, pairs, entries.get(mic)))

    # With one shared sequence this is no longer protected by a prefix in the
    # token -- it is protected by every venue drawing from the same counter. So
    # it is the check that actually proves the scheme, not a formality.
    if len(seen_tokens) > 1:
        total = sum(len(t) for t in seen_tokens.values())
        union = len(set().union(*seen_tokens.values()))
        checks.append(Check(
            date_dir, "*", "venues disjoint", total == union,
            f"{total - union:,} token(s) shared between venues "
            f"({', '.join(sorted(seen_tokens))})",
        ))

    issued = counter_token.load_sequence(date_dir)
    if issued is not None:
        highest = max((max((int(t) for t in tokens), default=0)
                       for tokens in seen_tokens.values()), default=0)
        checks.append(Check(
            date_dir, "*", "sequence covers", highest <= issued,
            f"sequence issued up to {issued:,}; highest token in any file "
            f"{highest:,}"
            + ("" if highest <= issued else
               " -- a file carries a token the sequence never issued, so a "
               "later day could hand that number to another instrument"),
        ))
    for check in checks:
        check.tag = V2
    return checks


def _check_manifest(date_dir, mic, cfg, pairs, entry) -> List[Check]:
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
    checks = []

    problems = []
    if int(entry.get("venue_id", 0)) != cfg.venue_id:
        problems.append(f"venue_id {entry.get('venue_id')} != config {cfg.venue_id}")
    if int(entry.get("count", -1)) != len(assigned):
        problems.append(f"count {entry.get('count')} != {len(assigned)} assigned")
    over = [t for t in assigned.values() if t > counter_token.INT32_MAX]
    if over:
        problems.append(f"{len(over):,} token(s) past int32")
    reissued = set(free) & set(assigned.values())
    if reissued:
        problems.append(f"{len(reissued):,} offset(s) in both free and assigned -- "
                        "the pool would hand out a live number")
    checks.append(Check(date_dir, mic, "manifest internal", not problems,
                        "; ".join(problems) or
                        f"highest {max(assigned.values(), default=0):,}, "
                        f"{len(assigned):,} assigned, "
                        f"{len(free):,} free"))

    # Every token in the file re-derived from the manifest offset. This is the
    # check that ties the two artefacts together; the rest only inspect one.
    missing = wrong = 0
    for script, token in zip(pairs.column("script").to_pylist(),
                             pairs.column("counterTokenV2").to_pylist()):
        offset = assigned.get(script)
        if offset is None:
            missing += 1
        else:
            try:
                rendered = counter_token.assign(offset)
            except ValueError:
                # An unissuable number in the manifest is exactly what
                # "does not re-derive" means; assign() refusing it is the
                # finding, not a reason to stop checking.
                rendered = None
            if rendered != token:
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
               "not a regression; a cross-date join on the token must carry the "
               "date with it"
               if ambiguous else ""),
            hard=False,
        ))
        checks.extend(check_pair_recycling(previous, current, mic))

        # Which day v2 actually chained from. Across a gap it silently reaches
        # further back, and the allocation it inherits is older than the data.
        cfg = _configured().get(mic)
        if cfg is not None and cfg.venue_id:
            try:
                source, stamp = counter_token.previous_tokens(
                    current, mic, cfg.venue_id)
            except ValueError as exc:
                checks.append(Check(span, mic, "chained from", False, str(exc)))
                continue
            checks.append(Check(
                span, mic, "chained from", source is not None and stamp == previous,
                f"carried from {stamp or 'nothing -- renumbered from scratch'}"
                + ("" if stamp == previous else
                   f", not from {previous}; the days between were numbered "
                   "against an older allocation"),
            ))
    for check in checks:
        check.tag = V2
    return checks


def run(dates: Sequence[str], venues: Sequence[str] = ()) -> int:
    """Validate each date, then each consecutive pair of them."""
    ordered = sorted(set(dates))
    checks: List[Check] = []
    for day in ordered:
        checks.extend(check_day(day, venues))
    for previous, current in zip(ordered, ordered[1:]):
        checks.extend(check_pair(previous, current, venues))
    return report(checks, suite="check-tokens")
