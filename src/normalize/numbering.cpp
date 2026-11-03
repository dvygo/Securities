#include "premarket/normalize/numbering.hpp"

#include <algorithm>
#include <set>

namespace premarket::numbering {

namespace {

std::vector<std::string> present_of(const std::vector<std::string>& scripts) {
    std::set<std::string> s;
    for (const auto& x : scripts)
        if (!x.empty()) s.insert(x);
    return {s.begin(), s.end()};
}

std::vector<std::int64_t> sorted_unique(const std::vector<std::int64_t>& v) {
    std::set<std::int64_t> s(v.begin(), v.end());
    return {s.begin(), s.end()};
}

std::vector<std::int64_t> minus_values(const std::vector<std::int64_t>& v, const TokenMap& remove) {
    std::set<std::int64_t> s(v.begin(), v.end());
    for (const auto& [k, t] : remove) s.erase(t);
    return {s.begin(), s.end()};
}

}  // namespace

Holdings Holdings::copy() const {
    Holdings h;
    h.venue_id = venue_id;
    h.assigned = assigned;
    h.free = sorted_unique(free);
    h.last_date = last_date;
    return h;
}

std::int64_t Holdings::highest() const {
    std::int64_t best = 0;
    for (const auto& [s, t] : assigned) best = std::max(best, t);
    for (auto t : free) best = std::max(best, t);
    return best;
}

std::optional<std::int64_t> DayAlloc::token_of(const std::string& script) const {
    if (auto it = assigned.find(script); it != assigned.end()) return it->second;
    if (auto it = retained.find(script); it != retained.end()) return it->second;
    return std::nullopt;
}

std::map<std::int64_t, std::string> DayAlloc::holders() const {
    std::map<std::int64_t, std::string> out;
    for (const auto& [s, t] : retained) out[t] = s;
    for (const auto& [s, t] : assigned) out[t] = s;
    return out;
}

std::optional<std::int64_t> Plan::token(const std::string& script) const {
    auto it = day.assigned.find(script);
    if (it == day.assigned.end()) return std::nullopt;
    return it->second;
}

std::string Plan::rendered(const std::string& script) const {
    auto it = day.assigned.find(script);
    return it == day.assigned.end() ? std::string() : counter_token::assign(it->second);
}

Plan allocate_live(const std::string& date, const std::vector<std::string>& scripts, const Holdings* state,
                   const DayAlloc* previous, Sequence& sequence, std::int64_t venue_id) {
    std::int64_t before = sequence.issued();
    auto present = present_of(scripts);
    std::set<std::string> here(present.begin(), present.end());
    TokenMap retained;
    counter_token::VenueTokens releasable;
    releasable.venue_id = venue_id;
    if (state != nullptr) {
        for (const auto& [s, t] : state->assigned) {
            if (here.count(s)) continue;
            auto ld = state->last_date.find(s);
            if (ld != state->last_date.end() && ld->second == date) retained[s] = t;
        }
        for (const auto& [s, t] : state->assigned)
            if (!retained.count(s)) releasable.assigned[s] = t;
        releasable.free = state->free;
    }
    std::optional<TokenMap> prefer;
    if (previous != nullptr) {
        prefer = previous->assigned;
        for (const auto& [s, t] : previous->retained)
            if (!prefer->count(s)) (*prefer)[s] = t;
    }
    auto tokens = counter_token::carry_forward(state != nullptr ? &releasable : nullptr, present, venue_id, sequence,
                                               prefer ? &*prefer : nullptr);

    Plan plan;
    plan.mode = LIVE;
    plan.date = date;
    plan.day.date = date;
    plan.day.assigned = tokens.assigned;
    plan.day.retained = retained;
    plan.day.free = minus_values(tokens.free, retained);

    std::map<std::string, std::string> last_date;
    for (const auto& [s, t] : tokens.assigned) last_date[s] = date;
    for (const auto& [s, t] : retained) {
        auto ld = state->last_date.find(s);
        last_date[s] = (ld != state->last_date.end() && !ld->second.empty()) ? ld->second : date;
    }
    plan.state_after.venue_id = venue_id;
    plan.state_after.assigned = tokens.assigned;
    for (const auto& [s, t] : retained) plan.state_after.assigned[s] = t;
    plan.state_after.free = minus_values(tokens.free, retained);
    plan.state_after.last_date = std::move(last_date);

    plan.exceptions = breaks(plan.day.assigned, retained, {previous});

    std::int64_t kept = 0, released = 0;
    if (state != nullptr) {
        for (const auto& s : present)
            if (state->assigned.count(s)) ++kept;
        for (const auto& [s, t] : state->assigned)
            if (!here.count(s) && !retained.count(s)) ++released;
    }
    auto n = static_cast<std::int64_t>(present.size());
    plan.counts = {{"scripts", n},
                   {"kept", kept},
                   {"arrived", n - kept},
                   {"retained", static_cast<std::int64_t>(retained.size())},
                   {"drawn", sequence.issued() - before},
                   {"released", released},
                   {"exceptions", static_cast<std::int64_t>(plan.exceptions.size())}};
    plan.neighbours = {{"earlier", previous != nullptr ? previous->date : ""}};
    plan.counter_before = before;
    plan.counter_after = sequence.issued();
    return plan;
}

Plan allocate_fill(const std::string& date, const std::vector<std::string>& scripts, const DayAlloc* own,
                   const DayAlloc* earlier, const DayAlloc* later, const Holdings* state, Sequence& sequence,
                   std::int64_t venue_id) {
    std::int64_t before = sequence.issued();
    auto present = present_of(scripts);
    Holdings base;
    if (state != nullptr) base = state->copy();
    else base.venue_id = venue_id;
    TokenMap day;
    std::set<std::int64_t> used;
    std::map<std::string, std::int64_t> counts = {{"own", 0},       {"from_later", 0}, {"from_earlier", 0},
                                                  {"from_state", 0}, {"from_free", 0},  {"fresh", 0},
                                                  {"retired", 0}};
    auto give = [&](const std::string& script, std::int64_t token, const std::string& source) {
        day[script] = token;
        used.insert(token);
        counts[source] += 1;
    };

    TokenMap retained;
    if (own != nullptr) {
        TokenMap mine = own->retained;
        for (const auto& [s, t] : own->assigned) mine[s] = t;
        for (const auto& s : present)
            if (auto it = mine.find(s); it != mine.end()) give(s, it->second, "own");
        for (const auto& [s, t] : mine)
            if (!day.count(s)) retained[s] = t;
        for (const auto& [s, t] : retained) used.insert(t);
    }

    const std::pair<const DayAlloc*, const char*> order[] = {{later, "from_later"}, {earlier, "from_earlier"}};
    for (const auto& [neighbour, source] : order) {
        if (neighbour == nullptr) continue;
        for (const auto& s : present) {
            if (day.count(s)) continue;
            auto it = neighbour->assigned.find(s);
            if (it == neighbour->assigned.end()) continue;
            if (!used.count(it->second)) give(s, it->second, source);
        }
    }
    std::vector<std::string> rest;
    for (const auto& s : present)
        if (!day.count(s)) rest.push_back(s);

    std::map<std::int64_t, std::string> blocked;
    for (const DayAlloc* neighbour : {earlier, later})
        if (neighbour != nullptr)
            for (const auto& [t, s] : neighbour->holders()) blocked[t] = s;

    auto usable = [&](const std::string& script, std::int64_t token) {
        if (used.count(token)) return false;
        auto it = blocked.find(token);
        return it == blocked.end() || it->second == script;
    };

    std::vector<std::int64_t> candidates;
    for (auto t : sorted_unique(base.free))
        if (!blocked.count(t)) candidates.push_back(t);
    std::size_t cursor = 0;
    std::set<std::int64_t> taken_from_pool;
    std::vector<std::int64_t> retired;
    std::sort(rest.begin(), rest.end());
    for (const auto& s : rest) {
        auto held_it = base.assigned.find(s);
        std::optional<std::int64_t> held;
        if (held_it != base.assigned.end()) held = held_it->second;
        if (held && usable(s, *held)) {
            give(s, *held, "from_state");
            continue;
        }
        while (cursor < candidates.size() && used.count(candidates[cursor])) ++cursor;
        if (cursor < candidates.size()) {
            std::int64_t token = candidates[cursor++];
            taken_from_pool.insert(token);
            give(s, token, "from_free");
        } else {
            give(s, sequence.take(), "fresh");
        }
        if (!held) {
            base.assigned[s] = day[s];
            base.last_date[s] = date;
        } else {
            retired.push_back(day[s]);
            counts["retired"] += 1;
        }
    }
    {
        std::set<std::int64_t> f(base.free.begin(), base.free.end());
        for (auto t : taken_from_pool) f.erase(t);
        base.free.assign(f.begin(), f.end());
    }

    Plan plan;
    plan.mode = FILL;
    plan.date = date;
    plan.exceptions = breaks(day, retained, {earlier, later});
    counts["scripts"] = static_cast<std::int64_t>(present.size());
    counts["exceptions"] = static_cast<std::int64_t>(plan.exceptions.size());
    counts["retained"] = static_cast<std::int64_t>(retained.size());
    counts["drawn"] = sequence.issued() - before;
    std::set<std::int64_t> held_now;
    for (const auto& [s, t] : day) held_now.insert(t);
    for (const auto& [s, t] : retained) held_now.insert(t);
    plan.day.date = date;
    plan.day.assigned = day;
    plan.day.retained = retained;
    for (auto t : sorted_unique(base.free))
        if (!held_now.count(t)) plan.day.free.push_back(t);
    plan.state_after = base;
    std::sort(retired.begin(), retired.end());
    plan.retired = retired;
    plan.counts = counts;
    plan.neighbours = {{"earlier", earlier != nullptr ? earlier->date : ""},
                       {"later", later != nullptr ? later->date : ""}};
    plan.counter_before = before;
    plan.counter_after = sequence.issued();
    return plan;
}

std::vector<TokenException> breaks(const TokenMap& day, const TokenMap& retained,
                                   const std::vector<const DayAlloc*>& neighbours_in) {
    std::map<std::int64_t, std::string> holders;
    for (const auto& [s, t] : retained) holders[t] = s;
    for (const auto& [s, t] : day) holders[t] = s;
    std::vector<const DayAlloc*> present;
    for (const auto* n : neighbours_in)
        if (n != nullptr) present.push_back(n);
    std::vector<TokenException> out;
    for (const auto& [s, token] : day) {
        for (const auto* neighbour : present) {
            auto it = neighbour->assigned.find(s);
            if (it == neighbour->assigned.end() || it->second == token) continue;
            std::vector<std::string> others;
            for (const auto* n : present)
                if (n != neighbour) others.push_back(n->date);
            std::string lost_to;
            if (auto h = holders.find(it->second); h != holders.end() && !h->second.empty()) lost_to = h->second;
            else lost_to = others.empty() ? "pool" : "chain:" + others[0];
            out.push_back({s, token, it->second, neighbour->date, lost_to});
        }
    }
    return out;
}

std::pair<std::optional<std::string>, std::optional<std::string>> neighbours(const std::string& date,
                                                                             const std::vector<std::string>& numbered) {
    std::optional<std::string> earlier, later;
    for (const auto& d : numbered) {
        if (d < date && (!earlier || d > *earlier)) earlier = d;
        if (d > date && (!later || d < *later)) later = d;
    }
    return {earlier, later};
}

}  // namespace premarket::numbering
