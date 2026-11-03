#pragma once
// premarket/qa.hpp -- check-tokens and check-state, held against what a numbering session
// actually writes. Ported from the QA half of 7.0.0's tests/test_numbering_session.py.
//
// check-tokens reads every day's normalized parquet (any <MIC>-*.parquet in
// data/<day>/TRANSFORM/normalized/) beside that day's allocation:
//   - a live chain passes, including "pool drained first" (freed numbers are reused before
//     the counter draws a new one), checked against the snapshot
//   - a fill whose clash is recorded in its exceptions file passes; its "stable" check says
//     the moves were recorded as exceptions, and the fill-only checks ("never releases",
//     "fill provenance", "filled from") run
//   - two tokens swapped in a normalized file with no recorded exception fail "stable"
//
// check-state reads the head and the run log:
//   - it passes after normal runs
//   - no head is a failure that names init-state
//   - a day numbered outside a session (a header written straight to disk) is caught twice:
//     the head's newest day is not the newest on disk, and the counter does not cover it.
//     The second needs counter_token::highest_issued() to read each header's
//     allocation.highest, not only _sequence.json -- the net for a run that published a
//     header and crashed before its sequence file caught up. This case found the 8.0.0 port
//     reading that field through a reference into a destroyed temporary (so it was ignored)
//   - a snapshot whose bytes changed is caught
//   - a START with no COMPLETE or FAIL ("runs closed") is a warning, not a failure: a run
//     killed by the scheduler leaves one and nothing is wrong with the numbering
//
// A hard failure renders as "<day> <venue> <name>: <detail>", the way the reports list it.
