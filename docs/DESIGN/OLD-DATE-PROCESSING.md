# ADR: old-date processing, the numbering state, and the load stage

- **Status:** accepted, 2026-09-25, released in 6.3.0
- **Supersedes:** numbering that chained each day to the previous one through a
  30-day lookback, and normalize-time pushes

## Context

A new host started producing data on 20260925. Its Fyers venues were numbered 1..138,744 as a first day.
Its Databento venues were not numbered yet. The next job was to add history: download and normalize older
dates.

The numbering could not do that safely. `opening_tokens`/`open_sequence` looked only *backwards*, at most
30 days, and had no global high-water mark. The failure went like this:

1. An older date found nothing before it and numbered from 1.
2. A later day whose venue was not yet numbered then chained from that older date.
3. The same trade date now carried duplicate tokens, across venues (XCME against XNSE's 1..103,390) and
   within a venue.

Downstream keys depend on `counterTokenV2`: the Postgres symbol master upserts on `(token, trade_date)`, and
MDF lanes map instrument ids to it. Both would have silently mis-associated instruments.

The pipeline is a security master. The standard held here is the one large data teams apply to one:

- identifiers that keep their meaning;
- runs serialised and audited;
- backups before any rewrite;
- quality gates;
- interlocks that keep old data out of live systems.

## Decisions

### The contract

These invariants are tested (including a property test over generated histories) and enforced by
`check-tokens` / `check-state`:

1. Within a trade date, no two instruments in any venue share a token, across every run of that date.
2. An instrument on two consecutive numbered days keeps its token, except where a fill recorded the break as
   an exception for exactly that pair.
3. The counter never issues the same number twice, across all runs and dates.
4. Re-running a day with the same inputs changes nothing and draws nothing.
5. A fill never changes or frees a token the state holds.
6. Per-day manifests and the run log are the record. The state is derivable from them.

### Numbering

- **One counter, never restarted.** New numbers are drawn above the highest of three values: the state
  head, every day's `_sequence.json`, and every header's `highest`. There is no window and no date order.
- **The state.** Each venue has one golden record in `data/_state/`: its current holdings, its free pool,
  and when each holding was last handed out. It is stored as snapshots written once and never modified; a
  head points at the current one. Live runs continue from it ("whatever ran last").
- **Two clocks.** The counter follows *transaction* time (runs). Which tokens a day starts from follows
  *valid* time (trading days).
- **Fills.** `normalize --dates` fills an older day from the numbered days either side of it:
  - A script on the later day keeps that token. The later day wins a clash.
  - A script only on the earlier day keeps that token.
  - Everyone else first gets their own state token if it is usable on this day, otherwise the lowest free
    number, otherwise a fresh one. Tokens a neighbour hands to someone else are never used.
  - A fill never releases anything and never alters an existing holding.
  - Breaks are computed from the finished day, so none can be missed. They are recorded per pair in
    `<MIC>.exceptions.parquet`. A later insertion supersedes a record rather than rewriting it.
- **Append-only within a date.** A token handed out on date D is never given to another instrument on D.
  Anything a later pass (or a second vendor for the same market) no longer carries is marked `retained`.
  It is freed only on the next date.
- **Returning scripts reclaim** their previous token before the pool's lowest.
- **Guards:**
  - A live run refuses a date older than the venue's newest numbered day.
  - A fill refuses a date that is not older than the newest live day.
  - A changed `venue_id` is refused.

### Operations

- **Commit protocol.** Each step makes a crash recoverable:
  1. Reserve the numbers.
  2. Publish the parquet.
  3. Stage the tables and the snapshot.
  4. **Replace the head** (the commit).
  5. Promote the staged files.
  6. Write the header.
  7. Log the run.

  Every command runs recovery first: it promotes, deletes or completes whatever an interrupted run left.
- **One run at a time.** A non-blocking file lock makes a second run refuse at once.
- **Backups** of every day's manifests and the head are taken before each fill and before `init-state`.
  The newest ten are kept. Snapshots are kept indefinitely.
- **Run log.** `runs.jsonl` is append-only, fsynced and OpenLineage-shaped (START/COMPLETE/FAIL, parent
  runs, inputs and outputs with digests). It also records the operator and a required `--reason` for
  fills.
- **Bootstrap.** `init-state` runs once per host. It refuses unless the venues' holdings are disjoint.
- **Fills are strict.** `--venue` is required. A refused venue or a missing input stops the run. Preview is
  `--dry-run`. The fill ends with a quality gate (check-tokens over the filled days and their neighbours,
  then check-state).

### The pipeline: download -> normalize -> load

- normalize only writes files.
- `load --sink NAME ...` delivers a day through named sinks, each `conf/sinks/<name>.ini`:
  - `clickhouse-normal`: the canonical schema.
  - `postgres-plugin`: one market, whose `venue_id` must match config.ini's.
  - `mdf-tokenmap`: a file sink.
- Interlocks: an older day never becomes "current". It loads dated ClickHouse tables only, and it is refused
  for MDF's delivery directory.
- The `plugin` command and normalize's push flags were removed. They exit with a pointer to `load`.

## Consequences

- Every host must run `init-state` once. Until then normalize and load refuse.
- Manifests are version 5 (`numbering` block, `retained` rows). Version 4 reads unchanged; older builds
  refuse version 5 rather than misreading it.
- The counter no longer restarts after a long gap, and live runs continue from the state across any gap.
  Previously a gap of more than 30 days renumbered the estate from 1.
- A missed day can now be filled. A few instruments may carry a recorded exception.
- `[clickhouse]` and `[postgres-plugin]` in config.ini are no longer read. Today's single multi-market
  `[postgres-plugin]` becomes one sink file per market, all pointing at the same table.

## Alternatives rejected

- **Continue a live run from the latest run's own day list.** A backfill of an older day would then
  become the anchor, and instruments listed after that day would get new tokens on the next live day.
  Replaced by a state that a fill only ever adds to.
- **Let old-only instruments take the tokens of later-only ones** (reuse in reverse). This puts two
  holdings on one token in the state. Fills take only genuinely free numbers.
- **Refuse fills between two numbered days.** Chosen first, then reversed: a missed day would stay
  unrecoverable. The two-sided fill with recorded exceptions replaced it.
- **One venue id per vendor for the same market.** The same instrument would get two tokens, and a
  mid-day vendor switch would put duplicates in Postgres. The venue id stays per *market*; vendors are
  sources.
- **Same-day recycling.** A re-run or a vendor switch would swap identities within a trade date.

## Evidence

- **Tests.** 514 pass, including:
  - a property test over 420 generated histories covering listings, expiries, one-day gaps, gap fills in
    both orders, re-runs and vendor switches;
  - mutation tests proving the property test catches broken rules;
  - a 300,000-instrument scale test;
  - crash tests at every commit step.
- **Two design bugs found before any code existed.** A simulation of 6,000 histories found (a) a break
  that moved to a different pair when gaps were filled newest-first, and (b) a fill overwriting a live
  holding.
- **The review found five more.** An independent design review found, among others, that the counter was
  written after the parquet was published.
- **Rehearsal on a copy of the host's data.** `init-state` ran from the Fyers manifests (counter 138,744).
  - Today's real XCME definitions (1,090,530 instruments) were numbered live as 138,745..1,229,274, and
    check-tokens reported the venues disjoint.
  - Same-day re-runs changed no token, and a killed run was closed by recovery.
  - A fill of a stand-in older day kept all 1,090,530 tokens and passed the gate (20/20 check-tokens,
    13/13 check-state).
  - The MDF delivery interlock refused an older day.
- **A quadratic membership test** was caught by the rehearsal's re-run (minutes, then stopped) and fixed.
  The scale test guards it.

## Follow-ups

- DuckDB as the repo standard for every read and query (scripts, QA, the load sinks), in its own branch.
- A vendor canonical model: per-vendor adapters, one shared record, and `venue`/`venueId`/`source` on every
  row.
- A `postgres-normal` sink.
- Off-box backups.
- Point-in-time reference data. Old days use today's config and basket lists.
- An ADR on identifier reuse. Industry masters never reuse ids; counterTokenV2 recycles within a venue.
- Reconcile CONTRIBUTING.md's Conventional Commits rule with the repo's `area: why` history.
