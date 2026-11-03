#pragma once
// premarket/normalize/numbering_session.hpp and the state store's operational half:
// init-state, the lock, the run log, backups and recovery. Ported from 7.0.0's
// tests/test_numbering_session.py.
//
// The tests drive a session the way a normalizer does -- venue(), plan(), reserve(),
// commit() -- against a throwaway tree that starts with 20260925's XNSE numbered 1..3, then
// read back what a later run, check-tokens or an auditor would read.
//
// init-state builds the head from the newest manifest of every venue, once; a dry run writes
// nothing; it refuses venues that share a token on a date (a day numbered in isolation would
// otherwise become every future day's collision) and it takes a backup and logs START and
// COMPLETE with the reason.
//
// Guards, each a refusal before anything is drawn:
//   - no head, no numbering ("run init-state")
//   - a second session while one holds data/_state/.lock ("another numbering run")
//   - a live run on a date older than the venue's newest numbered day ("use --dates")
//   - a fill on a date that is not older than the live day ("use --date-dir")
//   - a live run on a filled newest day, or older than it
//   - a venue_id that differs from the one the venue was numbered under
//
// The commit protocol, in order: reserve the counter (_sequence.json), stage the allocation,
// exceptions and snapshot as *.staged, write the head with a last_commit block naming every
// staged file and its sha256 (the commit point), promote the staged files, write the header,
// log COMPLETE. Crash tests stop the run at each step through util::failpoint:
//   - "write_head": before the commit point. The reserved numbers are leaked (the counter
//     never goes back), nothing else survives: the next session removes the staged files and
//     draws past the leak, and the run log holds one FAIL
//   - "write_header": after the commit point. recover() finishes the job from last_commit --
//     promotes and writes the header -- and every session runs recovery first, so nobody has
//     to remember to
//   - "promote", first call only: recover() promotes what is left
//   - "promote", every call, then a staged file is altered: recovery refuses on the digest
//     rather than promote bytes the commit did not write
//
// A fill takes a backup before it writes (pruned to the newest BACKUPS_KEEP = 10) and records
// its exceptions beside the allocation. The backup restores the manifests it took; the
// test extracts it with the system tar, which also proves the archive is ordinary
// ustar+gzip. A rerun that changes nothing points at the snapshot it already has.
//
// A preview (--dry-run of a fill) writes nothing, chains its own plans from one date to the
// next exactly as the real run would, and draws the same numbers the real run then draws.
