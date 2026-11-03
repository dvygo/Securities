#pragma once
// premarket/normalize/numbering.hpp -- how a venue-day gets its counterTokenV2.
//
// Pure functions: nothing here reads or writes a file. The state store supplies what a run
// starts from and persists what it produced; this module only decides, which is what lets
// the tests below throw thousands of generated histories at it.
//
// Two clocks. VALID time is the trading day a run describes, TRANSACTION time is when it
// runs. The counter follows transaction time (every run draws above everything ever issued);
// which tokens a day starts from follows valid time:
//   live (allocate_live)  the newest trading day, continued from the venue's latest state
//   fill (allocate_fill)  an older day, filled from the numbered days either side of it,
//                         never taking anything back from the state
//
// The contract both keep, and that the property test generates histories to break:
//   1. within a trade date no two scripts share a token, across every run of that date
//   2. a script on two consecutive numbered days keeps its token, except where a fill
//      recorded the break as an exception for exactly that pair of days
//   3. the counter never hands out the same number twice
//   4. re-running a day with the same inputs changes nothing and draws nothing
//   5. a fill never changes or frees a token the latest state holds
//
// Holdings   a venue's latest state; last_date is the newest trading day each holding was
//            handed out on, which is what makes numbering append-only within a date
// DayAlloc   one venue-day as its manifest records it: assigned (in this run's output),
//            retained (handed out earlier on the same date, absent now), free (the pool)
// TokenException  a script whose token differs from a neighbour's, on purpose; wanted_from
//            names the neighbour, lost_to who holds the wanted token ("chain:<date>" when the
//            other neighbour decided it, "pool" when nobody holds it)
// Plan       what a run decided; the session persists it, nothing here has been written
//
// allocate_live  carry_forward against the state: kept scripts keep the state's token,
//                departures release, arrivals reclaim their own previous token when it is
//                in the pool, then drain the pool, then draw. Holdings handed out on `date`
//                are never released on `date` (retained instead). A script on the previous
//                numbered day that ends up on a different token is recorded as an exception.
// allocate_fill  steps: 0 the day's own scripts keep their own tokens (re-run idempotence);
//                1 a script on the later day takes the later day's token (the later day
//                wins: that chain leads to today); 2 a script only on the earlier day takes
//                the earlier token; 3 a neighbour token already used here is a clash and
//                falls through; 4 everyone else, sorted: own state token when free to use
//                here, else the lowest state.free not held by either neighbour, else fresh.
//                Nothing is released and no existing holding changes; a script that held a
//                state token it could not use here takes a day-local number that is retired.
// breaks         every script whose token differs from a neighbour's assigned token,
//                computed from the finished day so it cannot miss one.
// neighbours     the nearest numbered days strictly before and after a date.
//
// The two 300k-script scale cases have a wall-clock budget (10 s each), so a sanitizer build
// (./build.sh sanitize, PREMARKET_SANITIZED=1) skips them: Debug + ASan runs them ~7x slower.
