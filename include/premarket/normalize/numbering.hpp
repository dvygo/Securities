#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "premarket/normalize/counter_token.hpp"

namespace premarket::numbering {

using counter_token::Sequence;
using counter_token::TokenMap;

inline constexpr const char* LIVE = "live";
inline constexpr const char* FILL = "fill";

struct Holdings {
    std::int64_t venue_id = 0;
    TokenMap assigned;
    std::vector<std::int64_t> free;
    std::map<std::string, std::string> last_date;

    Holdings copy() const;
    std::int64_t highest() const;
};

struct DayAlloc {
    std::string date;
    TokenMap assigned;
    TokenMap retained;
    std::vector<std::int64_t> free;

    std::optional<std::int64_t> token_of(const std::string& script) const;
    std::map<std::int64_t, std::string> holders() const;
};

struct TokenException {
    std::string script;
    std::int64_t token = 0;
    std::int64_t wanted = 0;
    std::string wanted_from;
    std::string lost_to;

    counter_token::ExceptionRow as_row() const { return {script, token, wanted, wanted_from, lost_to}; }
};

struct Plan {
    std::string mode;
    std::string date;
    DayAlloc day;
    Holdings state_after;
    std::vector<TokenException> exceptions;
    std::vector<std::int64_t> retired;
    std::map<std::string, std::int64_t> counts;
    std::map<std::string, std::string> neighbours;
    std::int64_t counter_before = 0;
    std::int64_t counter_after = 0;

    std::optional<std::int64_t> token(const std::string& script) const;
    std::string rendered(const std::string& script) const;
};

Plan allocate_live(const std::string& date, const std::vector<std::string>& scripts, const Holdings* state,
                   const DayAlloc* previous, Sequence& sequence, std::int64_t venue_id);

Plan allocate_fill(const std::string& date, const std::vector<std::string>& scripts, const DayAlloc* own,
                   const DayAlloc* earlier, const DayAlloc* later, const Holdings* state, Sequence& sequence,
                   std::int64_t venue_id);

std::vector<TokenException> breaks(const TokenMap& day, const TokenMap& retained,
                                   const std::vector<const DayAlloc*>& neighbours);

std::pair<std::optional<std::string>, std::optional<std::string>> neighbours(const std::string& date,
                                                                             const std::vector<std::string>& numbered);

}  // namespace premarket::numbering
