#pragma once
// premarket/normalize/mcx_contract.hpp -- MCX's own scrip file (MCXScrips.bcp inside the
// daily zip), the fallback when Fyers is not the XIMC feed.
//
// Cases recorded from 7.0.0 (mappers_golden.json["mcx"]): a gold future, a gold option
// (named by its own expiry month, strike in paise), a cotton future (agri: closes at 17:00
// IST whatever the season), cardamom, and a spot row in a group outside the table (no
// expiry, blank session). Prices stay in paise (SCALE 100) and the lot comes from column 20.
//
// Metals and energy follow US daylight saving: the session ends 23:55 IST while the US is on
// standard time and 23:30 IST while it is not. ["mcx_dst"] holds the days either side of
// both 2026 switches (8 March, 1 November) and Christmas.
