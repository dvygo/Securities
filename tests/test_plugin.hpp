#pragma once
// premarket/plugin.hpp -- the plugin contract table and the MDF token map.
//
// map_row() turns a normalized row into the 17 plugin columns. Cases recorded from 7.0.0
// (mappers_golden.json["plugin"]): 20 rows sampled from each of the three Fyers venues'
// normalized files of 20261103, and five hand-built US rows -- an XCME option and future,
// an XNAS equity with an unset expiry (UINT64_MAX), an XCBO put, and an XCME future that
// expired in 2020. is_expired() strips a contract only if it expired before 00:00 UTC on
// the trade date: contracts expiring on the day itself are live during its session.
//
// The token map (["tokenmap"]) is the binary file MDF loads: magic MDFVTOK1, format 1, a
// 64-byte header (venue, entry count, build time, FNV-1a of the body), then sorted
// (instrument id, token) pairs. Rows whose id or token does not parse, or is out of range,
// are skipped and counted, not guessed; the encoded bytes must match 7.0.0's exactly, and
// verify() must read back what encode() wrote.
