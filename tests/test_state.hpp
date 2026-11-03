#pragma once
// premarket/normalize/state.hpp -- the state store: the head, the per-venue snapshots and
// the counter floor. Ported from 7.0.0's tests/test_state.py.
//
// The head (data/_state/manifest.json) is the one file that says how far numbering got and
// which snapshot each venue continues from:
//   - no head reads as "no state"; require_head() says to run init-state, so a fresh host
//     knows the one command it needs
//   - a head it cannot read is refused, never treated as absent: starting over would
//     renumber every contract
//   - a 7.x head (version 1) is refused with the migrate-state command in the message; a
//     head newer than this binary (version 99) is refused by number
//   - it is written to a staging file and renamed, so a crash leaves the old head or the new
//     one, never half of either
//
// Snapshots (data/_state/<MIC>/<run>.alloc.parquet) hold a venue's holdings after a run:
//   - the same holdings give the same digest whatever the run, so an unchanged rerun points
//     at the snapshot it already has instead of writing a copy
//   - a snapshot is never overwritten with different content, and one whose bytes changed
//     after it was written (or that is missing) is refused on load -- the head records the
//     sha256 of each
//
// The counter floor is max(head counter, highest _sequence.json on disk): either alone is
// enough, so losing the head or every day directory still never reissues a token.
