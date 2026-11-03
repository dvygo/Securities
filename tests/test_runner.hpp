#pragma once
// premarket/runner.hpp -- the steps a command runs, and --only.
//
// normalize runs normalize-fyers, normalize-nse-contract, normalize-mcx-contract,
// normalize-databento and baskets, in that order, then csv-export (which does nothing
// without --csv). --only picks steps by name, in the pipeline's order whatever order they
// are given in; the aliases `fyers`, `databento` and `normalize` (every normalize step, which
// in 7.x left out normalize-mcx-contract) stand for their steps, and `all` for every step.
// A name that is no step is a usage error listing the ones that are: 7.x dropped it
// silently, so `--only baskets --only normalise-fyers` ran the baskets alone and exited 0.
// --venue limits the per-venue steps to the MICs named.
