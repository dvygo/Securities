#pragma once
// premarket/normalize/databento_norm.hpp -- a Databento definition file to canonical rows.
//
// Input is only ever DBN: the venue's day folder, data/YYYYMMDD/<MIC>/*.dbn[.zst], holding
// premarket's own live-replay download or an operator's file dropped there by hand. Files are
// read in name order; a file whose schema is not `definition`, or whose dataset is not the
// venue's, is skipped with a line in the log; instruments are deduplicated on instrument_id
// across all files, first file wins.
//
// definition_values renders every InstrumentDefMsg field into the def_* passthrough columns,
// as text, exactly as the 7.x normalized files carried them -- the downstream readers key on
// those strings:
//   - prices stay in the 1e-9 fixed point of the wire, timestamps in epoch nanoseconds;
//     unset values keep their sentinels (UNDEF_PRICE 9223372036854775807, UNDEF_TIMESTAMP
//     18446744073709551615)
//   - rtype is the word "instrument-def", not its number
//   - the one-character enums (security_update_action, match_algorithm,
//     user_defined_instrument, leg_instrument_class, leg_side) are that character, and an
//     unset one is a single NUL character, not an empty string
//   - fixed-width text is cut at its first NUL
//
// The three mappers derive the canonical columns:
//   map_xcme_row  GLBX: classification from instrument_class (F C P S T M X Y B K I), falling
//                 back to the " C6130"/" P8250" option suffix; expiration is the venue's own
//                 when set, else the contract month from the symbol (decade resolved against
//                 that expiration, or against the run date); brokerScript1 from the contract
//                 month, never from the last-trade date
//   map_xcbo_row  OPRA: OCC tail "YYMMDD[CP]########" (year 2000 + YY, strike / 1000),
//                 expiration at the US Eastern session close of the expiry date (17:00 ET for
//                 SPX/SPXW/VIX/RUT, 16:15 ET otherwise), instrument_class K rows are the
//                 underlying spots, typed SPOT/SPOT
//   map_xnas_row  equities; underlying_root keeps interior dots (BRK.B)
// A record with an empty raw_symbol gets the literal script "raw_symbol" (the old symbology
// fallback); the numbering pass skips it, so such a row carries no counterTokenV2.
