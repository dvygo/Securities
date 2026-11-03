#pragma once

#include <cstdint>
#include <string>

namespace premarket::session {

struct Ymd {
    int year;
    int month;
    int day;
};

Ymd parse_ymd(const std::string& yyyymmdd);

inline constexpr std::int64_t NS_SENTINEL_FLOOR_HI = 9223372036854775807LL;
inline constexpr const char* NSE_PREOPEN_UTC = "0330-0338";

std::int64_t as_ns(const std::string& value);
std::string ist_hhmm_to_utc(const std::string& ist_time);
std::string trading_session_ist_to_utc(const std::string& ist_session);
std::string trading_session_for_xnas(const Ymd& ref);
std::string trading_session_for_xcbo_equity(const Ymd& ref);
std::string trading_session_for_xcbo_index(const Ymd& ref);
std::string trading_session_for_xcme(const Ymd& ref);
std::int64_t xcbo_session_close_utc_seconds(const Ymd& expiry, bool is_index);

}  // namespace premarket::session
