#pragma once
// premarket/normalize/nse_contract.hpp -- NSE's own contract files (NSE_CM_security.csv,
// NSE_FO_contract.csv, NSE_CD_contract.csv), the fallback when Fyers is not the XNSE feed.
//
// Cases recorded from 7.0.0 (mappers_golden.json["nse_cash"] / ["nse_deriv"]):
//   - cash rows: an equity, an ETF (SctyTpFlg 4 with an INF ISIN), an InvIT, a block-deal
//     series sharing an ISIN with the equity (its type comes from the ISIN's other rows,
//     which is why types_by_isin() exists), a bond series, a sovereign gold bond, and a
//     row with no symbol, which maps to nothing
//   - derivative rows: an index option, an index future (strike -1, option XX), a currency
//     option with a fractional strike, and an empty row
// NSE dates are seconds since 1980-01-01 (NSE_EPOCH_OFFSET = 315532800 to Unix), and the
// expiry lands on the segment's session close.
