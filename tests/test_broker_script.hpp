#pragma once
// premarket/normalize/broker_script.hpp -- brokerScript1 for the US venues.
//
// GLBX (XCME) scripts become ROOT/MYY[/STRIKE{C|P}]: the month code stays, the year becomes
// two digits taken from the venue's expiry (so 0BZ0 expiring in 2031 is Z31, not Z20 or
// Z30). Spreads, user-defined instruments, bare roots and a zero expiry pass through
// unchanged. OCC (XCBO) symbols become ROOT/YYMMDD/STRIKE{C|P}, with the strike in its
// shortest exact form (format_strike: at most four decimals, no trailing zeros).
// Cases recorded from 7.0.0 (mappers_golden.json["broker"], ["occ"], ["strike"]).
