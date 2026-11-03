#pragma once
// premarket/normalize/fields.hpp -- the Fyers symbol-master mapper, one CSV row to one
// normalized row (the 24 leading columns) plus the segment MIC it belongs to.
//
// The cases in tests/data/mappers_golden.json["fyers"] were recorded from 7.0.0's
// fields.map_fyers_row: 50 rows sampled evenly from each of the six segment files of
// 20261103 (XNSE, XNFO, XNCD, XBSE, XBFO, XMCX -- equities, index and stock derivatives,
// currency, BSE, MCX), plus four rows built to hit the edges: a missing tick size and an
// unparseable one ("abc"), a trailing ':' on the trading session, an expiry in
// milliseconds and one in microseconds, -1 and 1234.565 strikes, lower-case "pe", "XX",
// "nan" and "1e-2". Every column of every case must match.
