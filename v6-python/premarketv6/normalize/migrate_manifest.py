"""One-shot conversion of v3 venue manifests to the v4 header + table pair.

v3 carried the whole {script: token} map inline in the header, which on an OPRA
day was 64 MB of JSON in an 87 MB directory. v4 moves that map into a Parquet
table beside the header and leaves the header small enough to read at a glance.

This is a migration, not a fallback. counter_token.venue_entry has no v3 reader
by design -- a silent fallback makes a day whose write failed indistinguishable
from a day that legitimately had no allocation. So the existing days are
converted once, here, and after that only v4 exists.

The conversion is pure format: the same VenueTokens goes in and comes out, which
is what verify() checks before anything is replaced. It does NOT re-run the
numbering, so no token can move.
"""
import json
import os
from pathlib import Path
from typing import List, Tuple

from . import counter_token


class ManifestMigrationFailed(RuntimeError):
    """A table did not read back as written. The v3 header was left in place."""


def v3_days(root: Path) -> List[Path]:
    """Every manifests/ directory holding at least one v3 header."""
    found = []
    for path in sorted(root.glob("*/v6/manifests")):
        if any(_is_v3(p) for p in sorted(path.glob("*.json"))
               if not p.name.startswith("_")):
            found.append(path)
    return found


def _is_v3(path: Path) -> bool:
    try:
        doc = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return int(doc.get("version", 0)) < 4 and "assigned" in (doc.get("allocation") or {})


def _v3_tokens(path: Path) -> counter_token.VenueTokens:
    """The VenueTokens a v3 header describes, read the way v3's reader did."""
    doc = json.loads(path.read_text())
    block = doc.get("allocation") or {}
    return counter_token.VenueTokens(
        venue_id=int(block.get("venue_id", 0)),
        assigned={str(k): int(v) for k, v in (block.get("assigned") or {}).items()},
        free=[int(x) for x in (block.get("free") or [])],
    )


def convert(header: Path, dry_run: bool = False) -> Tuple[str, int, int, int]:
    """Convert one v3 header in place. Returns (venue, assigned, free, bytes_saved).

    The table is written first and the header rewritten second, the same order
    write_venue_manifest uses and for the same reason: the header's presence is
    the completion signal, so it must never be the thing that exists alone.

    The run record is carried across verbatim. started_at/completed_at describe
    when the venue was normalized, which this does not change, so overwriting
    them with the migration's own clock would destroy the only record of it.
    """
    doc = json.loads(header.read_text())
    tokens = _v3_tokens(header)
    mic = str(doc.get("venue") or header.stem).upper()
    as_of = str(doc.get("date") or "")
    before = header.stat().st_size

    if dry_run:
        return mic, len(tokens.assigned), len(set(tokens.free)), 0

    alloc = counter_token.write_alloc(
        header.parent / f"{mic}{counter_token.ALLOC_SUFFIX}", tokens)

    # Proven BEFORE the v3 header is overwritten, because overwriting it is what
    # destroys the only other copy of this allocation. A table that does not read
    # back as what went in leaves the day exactly as it was found -- there are no
    # backups here by choice, so the ordering has to carry that weight itself.
    back_assigned, back_free = counter_token.read_alloc(alloc)
    if back_assigned != tokens.assigned or sorted(back_free) != sorted(set(tokens.free)):
        alloc.unlink(missing_ok=True)
        raise ManifestMigrationFailed(
            f"{mic} {as_of}: the allocation table did not read back as written "
            f"({len(back_assigned):,} assigned vs {len(tokens.assigned):,}, "
            f"{len(back_free):,} free vs {len(set(tokens.free)):,}). The v3 "
            f"manifest has been left untouched.")

    payload = {
        "version": counter_token.MANIFEST_VERSION,
        "date": as_of,
        "venue": mic,
        "started_at": doc.get("started_at", ""),
        "completed_at": doc.get("completed_at", ""),
        # The build that NUMBERED this day is not recoverable -- v3 headers did
        # not record one -- and stamping the migration's own sha here would
        # claim it was. "unknown" is the honest value; only days numbered by v4
        # onwards carry a real one.
        "code": {"build_sha": "unknown",
                 "manifest_version": counter_token.MANIFEST_VERSION,
                 "migrated_from": 3,
                 "migrated_by": counter_token.build_sha(),
                 "migrated_at": counter_token.utc_now()},
        "allocation": {
            "venue_id": tokens.venue_id,
            "highest": tokens.highest,
            "count": len(tokens.assigned),
            "free_count": len(set(tokens.free)),
            "path": alloc.name,
            "rows": len(tokens.assigned) + len(set(tokens.free)),
            "bytes": alloc.stat().st_size,
            "sha256": counter_token.sha256_of(alloc),
        },
        # v3 kept no run statistics. Left absent rather than zeroed: a zero
        # "drawn" is a claim about the numbering, and this knows nothing about it.
        "tokens": {},
    }
    staging = header.with_name(f"{header.name}.tmp.{__import__('os').getpid()}")
    staging.write_text(json.dumps(payload, indent=1, sort_keys=True))
    staging.replace(header)
    saved = before - (header.stat().st_size + alloc.stat().st_size)
    return mic, len(tokens.assigned), len(set(tokens.free)), saved


def verify(header: Path, expected: counter_token.VenueTokens) -> List[str]:
    """Read the converted pair back and hold it against what went in.

    The whole claim of this migration is that it moves bytes and changes no
    token. Anything short of comparing the full map would not test that claim.
    """
    as_of = json.loads(header.read_text()).get("date", "")
    mic = header.stem.upper()
    problems = []
    try:
        entry = counter_token.venue_entry(as_of, mic)
    except counter_token.ManifestCorrupt as exc:
        return [str(exc)]
    if entry.get("assigned") != expected.assigned:
        got, want = entry.get("assigned") or {}, expected.assigned
        moved = {s for s in want if got.get(s) != want[s]}
        problems.append(f"{len(moved):,} script(s) do not round-trip "
                        f"({len(got):,} read vs {len(want):,} expected)")
    if sorted(entry.get("free") or []) != sorted(set(expected.free)):
        problems.append(f"free pool differs: {len(entry.get('free') or []):,} "
                        f"read vs {len(set(expected.free)):,} expected")
    if int(entry.get("venue_id", -1)) != expected.venue_id:
        problems.append(f"venue_id {entry.get('venue_id')} != {expected.venue_id}")
    return problems


def run(dry_run: bool = False) -> int:
    """Bring every manifest under the data root up to the current shape.

    Two upgrades, both idempotent: v3 headers whose allocation is still inline
    become v4 pairs, and v4 headers whose tokens block predates the day/run
    split get that block rebuilt.
    """
    from .. import paths

    root = paths.data_root()
    days = v3_days(root)
    if not days:
        print("No v3 manifests found -- nothing to convert.")
        return rebuild_day_blocks(dry_run=dry_run)

    failures = 0
    total_saved = 0
    for directory in days:
        day = directory.parent.parent.name
        print(f"{day}")
        for header in sorted(directory.glob("*.json")):
            if header.name.startswith("_") or not _is_v3(header):
                continue
            expected = _v3_tokens(header)
            try:
                mic, assigned, free, saved = convert(header, dry_run=dry_run)
            except ManifestMigrationFailed as exc:
                print(f"  FAILED: {exc}")
                failures += 1
                continue
            if dry_run:
                print(f"  {mic:6} would convert {assigned:,} assigned, {free:,} free")
                continue
            problems = verify(header, expected)
            total_saved += saved
            status = "ok" if not problems else "FAILED: " + "; ".join(problems)
            failures += bool(problems)
            print(f"  {mic:6} {assigned:>9,} assigned, {free:>7,} free, "
                  f"{saved / 1e6:>7.1f} MB saved -- {status}")
    if not dry_run:
        print(f"\n{total_saved / 1e6:,.1f} MB saved, {failures} failure(s)")
    return (1 if failures else 0) or rebuild_day_blocks(dry_run=dry_run)


def needs_day_block(header: Path) -> bool:
    """True for a v4 header whose tokens block predates the day/run split."""
    try:
        doc = json.loads(header.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    tokens = doc.get("tokens") or {}
    return bool(tokens) and "day" not in tokens


def rebuild_day_block(header: Path) -> Tuple[str, dict]:
    """Recompute a header's tokens block in the day/run shape.

    Possible without re-normalizing because the day half is derived from durable
    data -- this venue's allocation, the previous day's, and the previous day's
    sequence high-water -- rather than from anything a run held in memory. That
    is the same property that makes the block survive a re-run.

    The run half cannot be recovered: nothing on disk records what a particular
    execution drew. It is written with the day's own figures for `drawn` and the
    sequence left where the day ended, and `anchored_on` empty to say so.
    """
    doc = json.loads(header.read_text())
    as_of = str(doc.get("date") or "")
    mic = str(doc.get("venue") or header.stem).upper()
    entry = counter_token.venue_entry(as_of, mic)
    tokens = counter_token.VenueTokens(
        venue_id=int(entry.get("venue_id", 0)),
        assigned={str(k): int(v) for k, v in (entry.get("assigned") or {}).items()},
        free=[int(x) for x in (entry.get("free") or [])])

    previous, carried = counter_token.previous_tokens(as_of, mic, tokens.venue_id)
    issued, _ = counter_token.previous_sequence(as_of)
    day = counter_token.day_stats(previous, tokens, issued or 0)

    doc["tokens"] = {
        "day": day,
        "carried_from": carried,
        "run": {"drawn": day["drawn"],
                "sequence_before": (issued or 0),
                "sequence_after": (issued or 0) + day["drawn"],
                "anchored_on": "",
                "sequence_from": ""},
    }
    staging = header.with_name(f"{header.name}.tmp.{os.getpid()}")
    staging.write_text(json.dumps(doc, indent=1, sort_keys=True))
    staging.replace(header)
    return mic, day


def rebuild_day_blocks(dry_run: bool = False) -> int:
    """Bring every flat tokens block up to the day/run shape."""
    from .. import paths

    found = [p for p in sorted(paths.data_root().glob("*/v6/manifests/*.json"))
             if not p.name.startswith("_") and needs_day_block(p)]
    if not found:
        print("Every tokens block already carries the day/run split.")
        return 0

    failures = 0
    for header in found:
        day_dir = header.parent.parent.parent.name
        if dry_run:
            print(f"  {day_dir} {header.stem}: would rebuild")
            continue
        try:
            mic, day = rebuild_day_block(header)
        except Exception as exc:                          # noqa: BLE001
            print(f"  {day_dir} {header.stem}: FAILED -- {exc}")
            failures += 1
            continue
        print(f"  {day_dir} {mic:6} arrived {day['arrived']:>7,}  "
              f"departed {day['departed']:>7,}  reused {day['reused']:>7,}  "
              f"drawn {day['drawn']:>7,}")
    if dry_run:
        print(f"\n{len(found)} would be rebuilt")
        return 0
    print(f"\n{len(found) - failures} rebuilt, {failures} failure(s)")
    return 1 if failures else 0
