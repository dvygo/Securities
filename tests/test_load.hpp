#pragma once
// premarket/load.hpp -- `premarket load`: named sinks, their validation, the old-day
// interlocks and the run log. Ported from 7.0.0's tests/test_load.py, without its fakes:
// the ClickHouse sink points at 127.0.0.1:1 (connection refused at once) and the
// mdf-tokenmap sink really writes token maps into the temp tree.
//
// A sink is <sinks_dir>/<name>.ini with one [sink] section, read by the strict v8 INI
// reader: an unknown key or a second section is an error, and nothing comes from the
// environment (7.x's PREMARKET_SINK_<NAME>_<KEY> overrides are gone). Keys per type:
//   clickhouse-normal  type host port database username password secure
//   postgres-plugin    type market venue_id database_url schema table create_table
//   mdf-tokenmap       type markets dir
// A missing sink names the ones that exist; postgres-normal is refused as planned, not
// built; a plugin sink pushes exactly one market, under the venue_id config.ini gives it,
// and only while that venue is enabled. Every shipped conf/sinks/*.ini.example, copied to
// .ini, loads against the shipped config.ini.example.
//
// A run loads every named sink before it pushes anything, so a typo in the second sink
// costs nothing in the first; it needs the state head; a dry run connects to nothing and
// logs nothing. Each sink is a child run in data/_state/runs.jsonl (START, one COMPLETE
// or FAIL per sink naming it, then the parent's COMPLETE or FAIL), and one failing sink
// fails the load without stopping the others.
//
// An older day never becomes what a live reader takes as current: delivering a token map
// into MDF's directory (dir = ...) for a day older than the newest live day is refused,
// while the same day's map may always be written into its own tree (no dir), for the
// enabled Databento venues only.
