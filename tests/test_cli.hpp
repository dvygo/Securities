#pragma once
// premarket/cli.hpp -- the command line, end to end.
//
// Global flags: --config=PATH is required for every command and --keys=PATH for the
// downloads; both must be absolute (a cron job or an HTCondor submit file runs from a
// directory nobody chose). Anything else wrong with the command line is a usage error,
// exit 2, with the reason on stderr: an unknown option (7.x's removed flags, e.g.
// --csv-only, are simply unknown now), a repeated one, a missing value.
//
// Every run is for one exchange (the user's rule: "no all run kind, just XNAS, XCBO, XIMC"):
// download, normalize, load, check-tokens and check-lineage need --venue MIC, exactly one,
// configured and enabled. A missing --venue, a list (XNSE,XBOM) or a second --venue is a usage
// error; 7.x's multi-venue commands (india, and xnas/xcme/xcbo in place of download) are
// unknown commands. --only picks among the venue's own steps, so naming another feed's step
// is refused. A feed = nse or mcx venue has nothing to download: the BOD process places its
// file. Only check-state, init-state and migrate-state are global, because the numbering
// state is one store shared by every venue.
//
// `normalize --dates=D1,D2 --venue MIC --reason "..."` is the manual backfill. It runs the
// real Fyers normalizer here, on the trimmed 7.0.0 CSVs in tests/data/state7x/20261103:
//   - scope and reason are required; without --venue (exit 2) or --reason (exit 1) it stops
//     before anything is read, saying which is missing
//   - it fills newest first, each date numbered from the one after it (the header's
//     numbering.neighbours.later), so a script keeps one token across the live day and both
//     filled days; the quality gate runs after and logs COMPLETE; the reason is in every
//     header it writes
//   - a named venue with no input on a date fails the run and stops there: older dates are
//     filled from that one, so nothing older is written, and the closing FAIL says no-input
//   - --dry-run is a preview: no data, no state (the command's own log file is expected)
