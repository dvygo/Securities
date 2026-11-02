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
    # A retained script held its token on this date just as an assigned one did;
    # the next day releases or keeps it exactly the same way, so for the pair
    # arithmetic it is one of the day's holdings.
    held = {str(k): int(v) for k, v in (entry.get("retained") or {}).items()}
    held.update({str(k): int(v) for k, v in (entry.get("assigned") or {}).items()})
    return {
        "assigned": held,
        "free": sorted(int(x) for x in (entry.get("free") or [])),
    }


def _numbering(date_dir: str, mic: str) -> dict:
    """The header's `numbering` block: how the session produced this venue-day.
    {} for a day numbered before the numbering session existed."""
    header = counter_token._read_json(counter_token.venue_manifest_path(date_dir, mic))
    return header.get("numbering") or {}


def _snapshot(mic: str, recorded: dict):
    """The state snapshot a header names, verified by its digest; None if none named."""
    from ..normalize import state
    if not recorded or not recorded.get("snapshot"):
        return None
    return state.load_snapshot(
        mic, state.VenueHead(0, recorded["snapshot"], recorded.get("sha256", ""), 0))


def _recorded_moves(previous: str, current: str, mic: str):
    """Scripts allowed to change token between the two days, and any read error.

    A fill (or a live day that could not reclaim) records every break against a
    neighbour. A record applies to the pair it names -- `wanted_from` is the
    other day -- so one written before a later fill landed between the two days
    is superseded, not applied.
    """
    allowed, problems = set(), []
    for day, other in ((current, previous), (previous, current)):
        try:
            rows = counter_token.read_exceptions(day, mic)
        except counter_token.ManifestCorrupt as exc:
            problems.append(str(exc))
            continue
        allowed |= {r["script"] for r in rows if r["wanted_from"] == other}
    return allowed, problems


def _recycling(span: str, mic: str, before: dict, after: dict, against: str) -> List[Check]:
    """carry_forward's three rules, held against the holdings the run started from."""
    kept = set(before["assigned"]) & set(after["assigned"])
    departed = set(before["assigned"]) - kept
    arrived = set(after["assigned"]) - kept
    released = sorted({before["assigned"][s] for s in departed})
    available = sorted(set(before["free"]) | set(released))
    available_set = set(available)
    taken = sorted(after["assigned"][s] for s in arrived
                   if after["assigned"][s] in available_set)
    extended = [s for s in arrived if after["assigned"][s] not in available_set]
    carried = sorted(available_set - set(taken))
    return [
        Check(span, mic, "offsets released",
              set(released).isdisjoint(after["assigned"].values()) or bool(taken),
              f"{len(departed):,} script(s) departed, {len(released):,} offset(s) "
              f"released; pool in {len(before['free']):,} -> {len(available):,} "
              f"available ({against})", tag=V2),
        Check(span, mic, "pool drained first",
              len(taken) == min(len(arrived), len(available)),
              f"{len(arrived):,} arrival(s): {len(taken):,} reused a released "
              f"token (available {len(available):,}), {len(extended):,} drew a "
              f"new one from the shared sequence", tag=V2),
        # A set, not a prefix: an arrival reclaiming its own previous token takes
        # that one rather than the pool's lowest, so what is left is "available
        # minus taken" and not "available after the first N".
        Check(span, mic, "pool carried", after["free"] == carried,
              f"{len(after['free']):,} offset(s) still free for tomorrow "
              f"(expected {len(carried):,})", tag=V2),
    ]


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
    return _recycling(span, mic, before, after, f"from {previous}")


def check_fill(date_dir: str, mic: str, block: dict) -> List[Check]:
    """A filled day, held against the state it started from and left.

    Two promises, both verifiable from what the fill recorded:

      never releases  every holding in the state before the fill is still held,
                      on the same token, after it -- a fill of an old day never
                      takes a live instrument's number
      provenance      every token on the day came from somewhere legitimate: a
                      neighbour, the script's own state holding, the free pool
                      the fill started from, or a fresh number above the counter
    """
    try:
        before = _snapshot(mic, block.get("state_before"))
        after = _snapshot(mic, block.get("state_after"))
    except Exception as exc:                                  # noqa: BLE001 - reported
        return [Check(date_dir, mic, "fill snapshots", False, str(exc), tag=V2)]
    checks = []
    if before is not None and after is not None:
        moved = [s for s, t in before.assigned.items() if after.assigned.get(s) != t]
        checks.append(Check(
            date_dir, mic, "never releases", not moved,
            f"{len(before.assigned):,} holding(s) before, {len(moved):,} changed or "
            f"released" + (f" (e.g. {moved[0]})" if moved else ""), tag=V2))
    entry, why = _entry(date_dir, mic)
    if why or not entry:
        return checks + [Check(date_dir, mic, "fill provenance", False,
                               why or "no allocation for a filled day", tag=V2)]
    neighbours = block.get("neighbours") or {}
    legit = set()
    for key in ("earlier", "later"):
        day = neighbours.get(key)
        if day:
            other, other_why = _entry(day, mic)
            if other_why or not other:
                checks.append(Check(date_dir, mic, "filled from", False,
                                    f"{key} neighbour {day} has no readable manifest "
                                    f"-- the fill's basis is gone", tag=V2))
                continue
            legit |= set(other["assigned"].values())
    if before is not None:
        legit |= set(before.assigned.values()) | set(before.free)
    floor = int(block.get("counter_before", 0))
    held = {**(entry.get("retained") or {}), **entry["assigned"]}
    stray = [s for s, t in held.items() if t not in legit and t <= floor]
    checks.append(Check(
        date_dir, mic, "fill provenance", not stray,
        f"{len(held):,} token(s): every one from a neighbour, the state, its free "
        f"pool, or above the counter ({floor:,})" if not stray else
        f"{len(stray):,} token(s) with no legitimate source, e.g. {stray[0]}", tag=V2))
    return checks


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
        block = _numbering(date_dir, mic)
        if block.get("mode") == "fill":
            checks.extend(check_fill(date_dir, mic, block))

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
    retained = {str(k): int(v) for k, v in (entry.get("retained") or {}).items()}
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
    doubled = set(retained.values()) & (set(assigned.values()) | set(free))
    if doubled:
        problems.append(f"{len(doubled):,} retained token(s) also assigned or free -- "
                        "a number already handed out on this date would be "
                        "handed out again")
    checks.append(Check(date_dir, mic, "manifest internal", not problems,
                        "; ".join(problems) or
                        f"highest {max(assigned.values(), default=0):,}, "
                        f"{len(assigned):,} assigned, "
                        + (f"{len(retained):,} retained, " if retained else "")
                        + f"{len(free):,} free"))

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
        f"{orphans:,} allocated to script(s) the file does not carry"
        + (f"; {len(retained):,} retained from an earlier run of the date, "
           "correctly absent" if retained else ""),
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
        moving = pc.not_equal(joined.column("token_a"), joined.column("token_b"))
        moved = set(joined.filter(moving).column("script").to_pylist())
        allowed, unreadable = _recorded_moves(previous, current, mic)
        for why in unreadable:
            checks.append(Check(span, mic, "exceptions readable", False, why))
        unrecorded = moved - allowed
        shared_scripts = set(joined.column("script").to_pylist()) if allowed else set()
        unmoved = (allowed & shared_scripts) - moved
        checks.append(Check(
            span, mic, "stable", not unrecorded and not unmoved,
            f"{shared:,} script(s) on both days, {len(moved):,} moved to a different "
            f"token"
            + (f" -- all {len(moved & allowed):,} recorded as exceptions for this "
               "pair" if moved and not unrecorded else "")
            + (f" -- {len(unrecorded):,} with no recorded exception, the "
               f"carry-forward chain is broken (e.g. {sorted(unrecorded)[0]})"
               if unrecorded else "")
            + (f" -- {len(unmoved):,} recorded as exceptions but did not move"
               if unmoved else ""),
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

        # Which rules apply depends on how each day was produced. A live day the
        # session numbered started from a state snapshot, so its pool arithmetic
        # is held against that snapshot rather than the previous day's table. A
        # filled day never releases, so it has no pool arithmetic of its own --
        # check_day holds it to "never releases" and "provenance" instead.
        was, now = _numbering(previous, mic), _numbering(current, mic)
        if now.get("mode") == "live" and (now.get("state_before") or {}).get("snapshot"):
            try:
                snapshot = _snapshot(mic, now["state_before"])
                start_from = {"assigned": dict(snapshot.assigned),
                              "free": sorted(snapshot.free)}
                checks.extend(_recycling(span, mic, start_from, _allocation(current, mic),
                                         "from the state it started from"))
            except Exception as exc:                          # noqa: BLE001 - reported
                checks.append(Check(span, mic, "recycling", False, str(exc)))
        elif not was and not now:
            checks.extend(check_pair_recycling(previous, current, mic))

        # Which day each chained from. A day the session numbered records it: a
        # live day continued the state; a fill names its neighbours. Only days
        # numbered before the session fall back to re-deriving the lookback.
        if was or now:
            filled_by = [(day, block) for day, block in ((previous, was), (current, now))
                         if block.get("mode") == "fill"]
            if filled_by:
                named = [set((block.get("neighbours") or {}).values()) - {""}
                         for _, block in filled_by]
                ok = any({previous, current} - {day} <= names
                         for (day, _), names in zip(filled_by, named))
                checks.append(Check(
                    span, mic, "filled from", ok,
                    "; ".join(f"{day} filled from {', '.join(sorted(n)) or 'nothing'}"
                              for (day, _), n in zip(filled_by, named))
                    + ("" if ok else " -- a day was numbered between them after the "
                       "fill; its records for this pair are superseded"),
                    hard=False))
            else:
                checks.append(Check(
                    span, mic, "chained from", now.get("mode") == "live",
                    f"{current} continued the venue's state"
                    + (f" (numbered by run {now.get('run_id', '')[:32]})" if now else "")))
            continue
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


def pairs_by_venue(dates: Sequence[str], venues: Sequence[str] = ()) -> Dict[str, List[str]]:
    """For each venue, the given dates on which it has a normalized file.

    Pairs are built per venue from these, not across the union of all venues'
    dates: a venue missing from the middle date would otherwise be compared
    across the gap with nothing, or not at all.
    """
    wanted = {v.upper() for v in venues}
    per_venue: Dict[str, List[str]] = {}
    for day in sorted(set(dates)):
        for mic in _venue_files(day):
            if not wanted or mic in wanted:
                per_venue.setdefault(mic, []).append(day)
    return per_venue


def collect(dates: Sequence[str], venues: Sequence[str] = ()) -> List[Check]:
    """Every check for the dates: each day, then each venue's consecutive pairs."""
    checks: List[Check] = []
    for day in sorted(set(dates)):
        checks.extend(check_day(day, venues))
    for mic, days in sorted(pairs_by_venue(dates, venues).items()):
        for previous, current in zip(days, days[1:]):
            checks.extend(check_pair(previous, current, [mic]))
    return checks


def run(dates: Sequence[str], venues: Sequence[str] = ()) -> int:
    """Validate each date, then each venue's consecutive pairs of them."""
    return report(collect(dates, venues), suite="check-tokens")
