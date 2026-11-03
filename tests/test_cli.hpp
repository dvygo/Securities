#pragma once
// premarket/cli.hpp -- the command line, end to end.
//
// Global flags: --config=PATH is required for every command and --keys=PATH for the
// downloads; both must be absolute (a cron job or an HTCondor submit file runs from a
// directory nobody chose). Anything else wrong with the command line is a usage error,
// exit 2, with the reason on stderr: an unknown option (7.x's removed flags, e.g.
// --csv-only, are simply unknown now), a repeated one, a missing value.
//
// `normalize --dates=D1,D2 --venue MIC --reason "..."` is the manual backfill. It runs the
// real Fyers normalizer here, on the trimmed 7.0.0 CSVs in tests/data/state7x/20261103:
//   - scope and reason are required; without --venue or --reason it stops before anything
//     is read, saying which is missing
//   - it fills newest first, each date numbered from the one after it (the header's
//     numbering.neighbours.later), so a script keeps one token across the live day and both
//     filled days; the quality gate runs after and logs COMPLETE; the reason is in every
//     header it writes
//   - a named venue with no input on a date fails the run and stops there: older dates are
//     filled from that one, so nothing older is written, and the closing FAIL says no-input
//   - --dry-run is a preview: no data, no state (the command's own log file is expected)
