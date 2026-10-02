# ADR: definitions from the live API only

- **Status:** accepted, 2026-10-02, released in 6.8.0
- **Supersedes:** the historical `definition` batch job (`_download_definitions_via_batch`),
  its `--dates` backfill and its `definition_ready_ratio` readiness check

## Context

The MDF lanes map each market-data record's `instrument_id` to our `counterTokenV2` with a token map,
built from the day's normalized definitions (`def_raw_instrument_id`, or `scriptToken` for XNAS). Until
6.8.0 those definitions came from a **historical** batch job.

Databento assigns `instrument_id` separately in its Historical and Live APIs. Support confirmed it on
2026-10-02: "for all datasets you should not assume the instrument ID assignment across the Historical and
Live APIs are consistent", and historical ids are never later changed to match live.

On OPRA the two diverged completely. Measured 2026-10-02 on the 8 XCBO parents:

| Live id compared with the historical file | Count |
|---|---|
| same id, same contract | 501 |
| id in the file, but naming a different contract | 29,545 |
| id not in the file | 25,006 |

A 42-second trace of the running XCBO lane matched each packet's prices to the contract they came from:
23,297 tokens carried another option's prices and 401 were right. This had been the case since 2026-09-29.
Historical renumbers OPRA daily while a live session keeps the numbering it started with on Monday, so the
two only agreed on the session's first day. GLBX and EQUS ids happened to agree every day.

## Decision

Definitions come from the live API only. `premarketv6 <venue>` subscribes to `definition` with `start=0`,
which replays everything the current session has sent, and writes the trade date's instruments as one DBN
file where the batch file used to go.

- **Today's instruments:** the last copy of each instrument, minus anything that expired before the trade
  date (`select_live_definitions`). For 2026-10-02 this gave exactly the batch's contract sets for OPRA
  (50,244) and EQUS (8). For GLBX it left out 1,171 user-defined spreads that had expired on earlier days,
  which the batch still carried.
- **The file:** written with its own DBN header (`encode_definition_file`), because a live session reports
  no schema or `stype_in`. Normalize reads it unchanged, and every output column keeps its name;
  `scriptToken` and `def_raw_instrument_id` now hold live ids.
- **Guards:** the trade date must be today (UTC). OPRA and EQUS are refused until that day's daily re-send has
  arrived. OPRA warns before its ~12:00Z new listings.
- **Numbering:** still keyed on the contract string, not on any id. A token can never move to another
  contract whatever Databento does with ids.

What a live session sends, measured 2026-09-27 to 2026-10-02 (US daylight time):

| Dataset | Session start | Daily re-send | New contracts |
|---|---|---|---|
| EQUS.MINI | Mon ~05:00Z | ~05:00Z, same ids | none |
| GLBX.MDP3 | Sun ~14:30Z | none | spreads, all day |
| OPRA.PILLAR | Mon 10:30Z | 10:30Z, same ids | ~12:00Z daily |

No live id changed within that week.

## Consequences

- There is no back-dating: a live session only describes the current week, so a missed day cannot be fetched
  later. `normalize --dates` (fill) still works on files already on disk.
- The daily run belongs after 12:00Z (17:30 IST) and before the 13:30Z US open:
  `docs/PREMARKET-RUNBOOK.md`.
- Whether live ids change at the weekly gateway restart is not established. A daily fetch after the session
  start follows them either way. On a Monday, check the XCBO lane's `tob_untokenized` after the restart.
- After the fix, the same trace gave 50,244 of 50,244 contracts matching the live session, nothing dropped by
  the lane, and 0 tokens with another contract's prices.
