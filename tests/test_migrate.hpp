#pragma once
// premarket/normalize/migrate.hpp -- `premarket migrate-state`, the one step between a 7.x
// host and 8.0.0.
//
// tests/data/state7x/ is a real 7.0.0 tree, not a hand-written imitation: 7.0.0 ran
// init-state and then normalize-fyers on two days of trimmed Fyers files (eight rows per
// segment; the second day drops two rows and adds two). It holds the v1 head, the v5 venue
// headers, the allocation parquets, the per-venue snapshots, runs.jsonl and the second day's
// raw CSVs. Everything else 7.0.0 wrote (normalized parquet, the backup, .lock) is left out.
//
//   - 8.0.0 refuses the tree until it is migrated, and the refusal names the command
//   - a dry run reads everything, reports what it would do and writes nothing
//   - the migration moves the head to v8, every header to v8 with a "migrated" block
//     (from_version 5, by, at, run_id) and every _sequence.json to v8; it changes no token,
//     no free list, no retained map, no snapshot and no counter, takes a backup first, and
//     logs START and COMPLETE as job migrate-state. check-state passes on the result
//   - a second run sees the v8 head and does nothing
//   - 8.0.0 then carries the 7.x numbering on: re-running the 7.x day reproduces its
//     tokens exactly, and the next day keeps every surviving script's token
