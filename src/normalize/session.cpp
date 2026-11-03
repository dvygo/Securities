#include "premarket/normalize/session.hpp"

#include <date/tz.h>

#include <array>
#include <chrono>

#include "premarket/util.hpp"

namespace premarket::session {

namespace {

struct Window {
    int sh, sm, eh, em;
};

using Profile = std::array<Window, 3>;

constexpr Profile kXnas = {{{4, 0, 9, 30}, {9, 30, 16, 0}, {16, 0, 20, 0}}};
constexpr Profile kXcboEquity = {{{7, 30, 9, 25}, {9, 30, 16, 0}, {16, 0, 16, 15}}};
constexpr Profile kXcboIndex = {{{20, 15, 9, 25}, {9, 30, 16, 15}, {16, 15, 17, 0}}};
constexpr Profile kXcme = {{{18, 0, 9, 30}, {9, 30, 16, 15}, {16, 15, 17, 0}}};

const date::time_zone* eastern() {
    static const date::time_zone* zone = date::locate_zone("America/New_York");
    return zone;
}

std::chrono::sys_seconds eastern_to_utc(int y, int m, int d, int hh, int mm) {
    date::local_seconds local = date::local_days{date::year{y} / date::month{static_cast<unsigned>(m)} /
                                                 date::day{static_cast<unsigned>(d)}} +
                                std::chrono::hours{hh} + std::chrono::minutes{mm};
    auto info = eastern()->get_info(local);
    auto offset = info.first.offset;
    if (info.result == date::local_info::nonexistent) offset = info.first.offset;
    return std::chrono::sys_seconds{local.time_since_epoch() - offset};
}

std::string hhmm(std::chrono::sys_seconds t) {
    auto day = date::floor<date::days>(t);
    date::hh_mm_ss<std::chrono::seconds> tod{t - day};
    return util::zero_pad(tod.hours().count(), 2) + util::zero_pad(tod.minutes().count(), 2);
}

std::string slot(const Ymd& ref, const Window& w) {
    auto start = eastern_to_utc(ref.year, ref.month, ref.day, w.sh, w.sm);
    Ymd end_day = ref;
    if (w.eh * 60 + w.em <= w.sh * 60 + w.sm) {
        date::year_month_day next{date::sys_days{date::year{ref.year} / date::month{static_cast<unsigned>(ref.month)} /
                                                 date::day{static_cast<unsigned>(ref.day)}} +
                                  date::days{1}};
        end_day = {static_cast<int>(next.year()), static_cast<int>(static_cast<unsigned>(next.month())),
                   static_cast<int>(static_cast<unsigned>(next.day()))};
    }
    auto end = eastern_to_utc(end_day.year, end_day.month, end_day.day, w.eh, w.em);
    return hhmm(start) + "-" + hhmm(end);
}

std::string profile_utc(const Ymd& ref, const Profile& p) {
    return slot(ref, p[0]) + "|" + slot(ref, p[1]) + "|" + slot(ref, p[2]);
}

}  // namespace

Ymd parse_ymd(const std::string& s) {
    if (!util::valid_yyyymmdd(s)) throw ValueError("time data " + util::quoted(s) + " does not match format '%Y%m%d'");
    return {std::stoi(s.substr(0, 4)), std::stoi(s.substr(4, 2)), std::stoi(s.substr(6, 2))};
}

std::int64_t as_ns(const std::string& value) {
    if (value.empty()) return 0;
    std::string t = util::strip(value);
    bool negative = !t.empty() && t[0] == '-';
    std::string digits = (!t.empty() && (t[0] == '-' || t[0] == '+')) ? t.substr(1) : t;
    if (util::is_digits(digits)) {
        if (negative) return 0;
        if (digits.size() > 19) return 0;
        if (digits.size() == 19 && digits > "9223372036854775807") return 0;
        return std::stoll(digits);
    }
    auto f = util::parse_float(value);
    if (!f || std::isnan(*f) || std::isinf(*f)) return 0;
    double truncated = std::trunc(*f);
    if (truncated < 0 || truncated >= 9.2233720368547758e18) return 0;
    return static_cast<std::int64_t>(truncated);
}

std::string ist_hhmm_to_utc(const std::string& ist_time) {
    auto parts = util::split(ist_time, ":");
    if (parts.size() != 2) return "";
    auto hour = util::parse_int(parts[0]);
    auto minute = util::parse_int(parts[1]);
    if (!hour || !minute || *hour < 0 || *hour > 23 || *minute < 0 || *minute > 59) return "";
    int total = static_cast<int>(*hour) * 60 + static_cast<int>(*minute) - (5 * 60 + 30);
    total = ((total % 1440) + 1440) % 1440;
    return util::zero_pad(total / 60, 2) + ":" + util::zero_pad(total % 60, 2);
}

std::string trading_session_ist_to_utc(const std::string& ist_session) {
    std::string s = util::rstrip_chars(util::strip(ist_session), ":");
    if (s.empty()) return "";
    std::vector<std::string> slots;
    for (auto part : util::split(s, "|")) {
        part = util::strip(part);
        auto dash = part.find('-');
        if (part.empty() || dash == std::string::npos) return "";
        std::string start = part.substr(0, dash), end = part.substr(dash + 1);
        auto take = [](const std::string& x) {
            std::string head = x.substr(0, std::min<std::size_t>(2, x.size()));
            std::string tail = x.size() > 2 ? x.substr(2) : "";
            return head + ":" + tail;
        };
        std::string u_start = ist_hhmm_to_utc(take(start));
        std::string u_end = ist_hhmm_to_utc(take(end));
        if (u_start.empty() || u_end.empty()) return "";
        slots.push_back(util::replace_all(u_start, ":", "") + "-" + util::replace_all(u_end, ":", ""));
    }
    return util::join(slots, "|");
}

std::string trading_session_for_xnas(const Ymd& ref) { return profile_utc(ref, kXnas); }
std::string trading_session_for_xcbo_equity(const Ymd& ref) { return profile_utc(ref, kXcboEquity); }
std::string trading_session_for_xcbo_index(const Ymd& ref) { return profile_utc(ref, kXcboIndex); }
std::string trading_session_for_xcme(const Ymd& ref) { return profile_utc(ref, kXcme); }

std::int64_t xcbo_session_close_utc_seconds(const Ymd& expiry, bool is_index) {
    const auto& last = (is_index ? kXcboIndex : kXcboEquity)[2];
    return eastern_to_utc(expiry.year, expiry.month, expiry.day, last.eh, last.em).time_since_epoch().count();
}

}  // namespace premarket::session
