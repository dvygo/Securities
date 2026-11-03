#pragma once
// premarket/sources/databento.hpp -- `premarket xnas|xcme|xcbo`, the live definition replay.
//
// A download subscribes to the dataset's definition schema from the start of the session,
// waits for Databento's "Finished definition replay", and writes what it received as one
// zstd DBN v3 file. What it keeps, and the parts testable without the network:
//   - the venue's symbols file is one symbol per line, blank lines ignored; under
//     stype_in = parent a bare root gets ".OPT" (SPY -> SPY.OPT), while ".FUT", ".OPT",
//     ".SPOT" and dotted index names stay as written. No file named = every instrument
//     (ALL_SYMBOLS); a named file that is missing is an error, not "everything"
//   - a replay repeats an instrument when its definition changed during the session: the
//     last copy wins, and a contract that expired before the session began is dropped. An
//     unset expiration (UINT64_MAX: equities, OPRA's spot legs) never expires
//   - the file written is DBN v3, and it reads back through the same reader normalize uses
//   - OPRA lists the day's new contracts at about 08:00 New York time, which is 12:00Z in
//     US summer and 13:00Z in winter. A download before then is warned about; 7.x warned at
//     a fixed 12:00Z, so in winter a 12:30Z download missed the listings silently
