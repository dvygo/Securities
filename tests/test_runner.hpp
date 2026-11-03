#pragma once
// premarket/runner.hpp -- the steps a run is made of, for its one venue.
//
// normalize --venue MIC runs the venue's own normalize step, chosen by its feed
// (fyers -> normalize-fyers, nse -> normalize-nse-contract, mcx -> normalize-mcx-contract,
// databento -> normalize-databento), then baskets if the venue has any (basket names start
// with their MIC: XNSE_NIFTYFNO_*) and [baskets] enabled = 1, then csv-export (which does
// nothing without --csv). --only picks among those, in that order whatever order it is
// given in. There are no aliases and no `all`: a name that is not one of the venue's steps
// is a usage error listing the ones that are, so a typo cannot run nothing and exit 0 (7.x
// dropped unknown names silently).
//
// download --venue MIC is one step: the Databento live replay or the venue's Fyers
// segments. A feed = nse or mcx venue has nothing to download; its file is placed by the BOD
// process, and asking to download it is an error that says where the file goes.
