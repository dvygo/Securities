#pragma once
// premarket/util.hpp -- small helpers every module leans on.
//
// Error types
//   ValueError / RuntimeError / FileNotFoundError classify failures the way callers react to
//   them: the normalizers catch ValueError around numbering and skip the venue, so
//   ManifestCorrupt, StateCorrupt and NumberingRefused all derive from it. SystemExit is not a
//   std::exception on purpose: a step's catch-all (runner::run) must not swallow it.
//
// Strings
//   strip/lstrip/rstrip trim ASCII whitespace (space, \t \n \r \v \f).
//   split(s, sep) keeps empty fields; split_ws collapses runs of whitespace; splitlines breaks
//   on \n, \r\n and \r. quoted() is the form error messages use: 'text', or "text" when the
//   text holds a single quote.
//
// Numbers -- the vendor files are text, and these decide what a cell means:
//   parse_int        whole number with optional sign and surrounding space; "010" is 10,
//                    "1.0" is not a whole number
//   parse_float      decimal or exponent form, inf/nan; hex is refused ("0x10" is not a price)
//   parse_int_truncating  the float, truncated toward zero (a Fyers lot of "75.0" is 75)
//   round_half_even  rounding of a scaled price: half to even on the binary value, so
//                    1.005 * 100 (100.49999999999999) is 100
//   commas           1234567 -> "1,234,567", for log lines
//
// Files
//   staging_path is "<name>.tmp.<pid>": every writer stages under it and renames into place,
//   so a finished file never shares a name with a run in progress.
//   write_file_fsync + fsync_dir make a rename durable (the head's commit point).
//   relative_to throws ValueError when `path` is not under `base`.
//
// Identity and time
//   username: LOGNAME, USER, LNAME, USERNAME, then the passwd entry.
//   utc_now_iso "%Y-%m-%dT%H:%M:%SZ"; utc_stamp_micro "%Y%m%dT%H%M%S" + microseconds + "Z"
//   (run ids sort by transaction time). add_days / days_between / day_start_ns are UTC
//   calendar arithmetic on YYYYMMDD.
