"""counterTokenV3: a token issued once per instrument and never reissued.

WHY A THIRD SCHEME

counterToken (v1) is positional -- a script's number is its row index within
the day, so it moves whenever the day's row order shifts. counterTokenV2 is
stable across days, but it earns that stability by deriving each day from the
previous day's allocation, which makes it a function of PROCESSING ORDER as
well as of the data. Two consequences follow, and neither is fixable inside
that design:

  A skipped day cannot be backfilled. Day N+1 chains off whatever manifest
  existed when it ran, so inserting day N afterwards leaves the two
  disagreeing. Measured on a 12-day simulation with one gap: 4 scripts changed
  token between consecutive days, and 8 tokens named a DIFFERENT script on
  consecutive days -- the second is worse, because a cross-date join then
  returns wrong rows rather than no rows.

  Numbers are recycled. When a script stops appearing its offset returns to a
  free pool and the next new script takes it. That is what makes a token
  ambiguous over time.

v3 fixes both by not computing anything. A token is looked up; if the
instrument has never been seen, one is appended. Nothing about an existing
token depends on when it was assigned, what ran before, or what ran after, so
backfilling a gap is an ordinary lookup and re-running any day is a no-op.

This is the ordinary shape for instrument identifiers: FIGI, ISIN and CUSIP are
all registry-issued and never reused, and none of them is derived by
recomputation. An identifier that CAN be recomputed can be recomputed
differently.

WHY NOT A PREFIXED NUMBER LIKE v1/v2

v1 and v2 carry a two-digit venue prefix, and venue_id owns venue_id and
venue_id+1. The venues are spaced two apart, so a v3 at venue_id+2 lands on the
next venue's v1 every time (XCBO would take 12, which is XCME's). Re-spacing
them would renumber v1 and v2, which the plugin and Postgres paths depend on.

So v3 does not encode the venue at all. Uniqueness comes from the registry
being a single sequence rather than from partitioned number space -- which is
also why it has no per-venue capacity ceiling to exhaust, unlike v1/v2's
11,111,110 rows per prefix inside int32.

Tokens start at V3_BASE, far above anything int32 can hold, so a v3 token can
never be mistaken for a v1 or v2 one by a reader that has both.
"""
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

# First token issued. Above int32's 2,147,483,647 by three orders of magnitude,
# so v1/v2 and v3 tokens can never be confused for one another, and comfortably
# inside int64 (9.2e18) -- at a million new instruments a day this base leaves
# room for over 25 billion years.
V3_BASE = 1_000_000_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument (
    venue       TEXT    NOT NULL,
    script      TEXT    NOT NULL,
    token       INTEGER NOT NULL UNIQUE,
    first_seen  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (venue, script)
);
CREATE INDEX IF NOT EXISTS instrument_token ON instrument(token);
"""


class TokenRegistry:
    """Append-only (venue, script) -> token store backed by SQLite.

    SQLite rather than the per-day JSON manifests v2 uses: those are rewritten
    whole every day (84 MB for one 2026-08-26 run) purely to carry state to
    tomorrow, and they cannot be looked up without parsing all of it. A registry
    is queried, not replayed, and one transaction per venue keeps two venues
    normalising at once from interleaving.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn:
            # WAL so a reader is not blocked by the writer; two venues are
            # normalised in the same run and may overlap.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    def tokens_for(self, venue: str) -> Dict[str, int]:
        """Every token already issued for a venue."""
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT script, token FROM instrument WHERE venue = ?", (venue,)
            ).fetchall()
        return {script: token for script, token in rows}

    def assign(self, venue: str, scripts: Iterable[str], as_of: str,
               first_seen: Optional[Mapping[str, str]] = None) -> Dict[str, int]:
        """Return {script: token} for `scripts`, issuing tokens for new ones.

        Idempotent: a script already in the registry keeps its token, so calling
        this twice for the same day -- or for a day that ran months ago --
        returns the same mapping and writes nothing the second time.

        New scripts are taken in sorted order so a first run is reproducible
        rather than dependent on the order rows happened to arrive. That only
        affects which new script gets which number on the day it first appears;
        it can never move a token that was already issued.

        `first_seen` optionally supplies each script's own start of life --
        def_activation, for the Databento venues, which is the venue's answer
        rather than ours and does not change with when we ran. Falls back to
        `as_of` for a feed that has no such field.
        """
        wanted = sorted({s for s in scripts if s and s.strip()})
        if not wanted:
            return {}

        first_seen = first_seen or {}
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # IMMEDIATE takes the write lock up front, so two venues cannot both
            # read the same MAX(token) and then both try to use it.
            conn.execute("BEGIN IMMEDIATE")
            try:
                known = dict(conn.execute(
                    "SELECT script, token FROM instrument WHERE venue = ?", (venue,)
                ).fetchall())
                fresh = [s for s in wanted if s not in known]
                if fresh:
                    row = conn.execute("SELECT MAX(token) FROM instrument").fetchone()
                    next_token = (row[0] or V3_BASE - 1) + 1
                    conn.executemany(
                        "INSERT INTO instrument (venue, script, token, first_seen, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(venue, s, next_token + i, first_seen.get(s, as_of), as_of)
                         for i, s in enumerate(fresh)],
                    )
                    known.update({s: next_token + i for i, s in enumerate(fresh)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {s: known[s] for s in wanted}

    def stats(self) -> Dict[str, int]:
        with closing(sqlite3.connect(self.path)) as conn:
            total = conn.execute("SELECT count(*) FROM instrument").fetchone()[0]
            venues = dict(conn.execute(
                "SELECT venue, count(*) FROM instrument GROUP BY venue").fetchall())
        return {"total": total, **venues}
