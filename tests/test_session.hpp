#pragma once
// premarket/normalize/session.hpp -- tradingSessionUTC.
//
// India: Fyers gives sessions in IST ("0915-1530|1815-1915:"); they become UTC by taking
// 05:30 off each end. A trailing ':' is dropped, a three-digit time or 2400 is not a time
// (the whole session becomes blank rather than half-converted), surrounding spaces are
// trimmed, and 00:00 IST is 18:30 the day before. Recorded from 7.0.0 (["ist"]).
//
// US: the session for each venue depends on whether the US is on daylight time on the
// reference day; ["us_sessions"] covers both sides of the 8 March and 1 November 2026
// switches. xcbo_session_close_utc_seconds() is the expiry instant for an OCC option: 16:00
// New York for equities and 16:15 for indexes on the expiry date.
