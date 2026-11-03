#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "premarket/config.hpp"
#include "premarket/jsonio.hpp"
#include "premarket/util.hpp"

namespace premarket::counter_token {

namespace fs = std::filesystem;
using Json = jsonio::Json;
using TokenMap = std::map<std::string, std::int64_t>;

inline constexpr std::int64_t INT32_MAX_TOKEN = 2147483647;
inline constexpr std::int64_t FIRST_TOKEN = 1;
inline constexpr int MANIFEST_LOOKBACK_DAYS = 30;
inline constexpr int MANIFEST_VERSION = 8;
inline constexpr const char* ALLOC_SUFFIX = ".alloc.parquet";
inline constexpr const char* EXCEPTIONS_SUFFIX = ".exceptions.parquet";
inline constexpr const char* ALLOC_ASSIGNED = "assigned";
inline constexpr const char* ALLOC_FREE = "free";
inline constexpr const char* ALLOC_RETAINED = "retained";

struct ManifestCorrupt : ValueError {
    using ValueError::ValueError;
};

std::string assign(std::int64_t n);
std::int64_t capacity();
void check_capacity(const std::string& mic, std::int64_t issued, std::int64_t arriving);
std::map<std::string, std::vector<std::string>> validate(const std::map<std::string, config::Venue>& venues);

struct VenueTokens {
    std::int64_t venue_id = 0;
    TokenMap assigned;
    std::vector<std::int64_t> free;
    TokenMap retained;

    std::string token(const std::string& script) const;
    std::int64_t highest() const;
};

class Sequence {
public:
    explicit Sequence(std::int64_t issued = FIRST_TOKEN - 1) : issued_(issued), start_(issued) {}
    std::int64_t take();
    std::int64_t issued() const { return issued_; }
    std::int64_t start() const { return start_; }
    std::int64_t drawn() const { return issued_ - start_; }

private:
    std::int64_t issued_;
    std::int64_t start_;
};

VenueTokens carry_forward(const VenueTokens* previous, const std::vector<std::string>& scripts,
                          std::int64_t venue_id, Sequence& sequence, const TokenMap* prefer = nullptr);

fs::path manifests_dir(const std::string& as_of);
fs::path venue_manifest_path(const std::string& as_of, const std::string& mic);
fs::path alloc_path(const std::string& as_of, const std::string& mic);
fs::path exceptions_path(const std::string& as_of, const std::string& mic);
fs::path sequence_path(const std::string& as_of);

Json read_json(const fs::path& path);
void require_current(const Json& doc, const fs::path& path);
std::string sha256_of(const fs::path& path);
std::string build_sha();

std::int64_t jint(const Json& v, std::int64_t fallback = 0);
std::string jstr(const Json& v, const std::string& fallback = "");
const Json& jget(const Json& obj, const std::string& key);
bool truthy(const Json& v);

fs::path write_alloc(const fs::path& path, const VenueTokens& tokens);

struct Allocation {
    TokenMap assigned;
    std::vector<std::int64_t> free;
    TokenMap retained;
};
Allocation read_allocation(const fs::path& path);
std::pair<TokenMap, std::vector<std::int64_t>> read_alloc(const fs::path& path);

struct VenueEntry {
    std::int64_t venue_id = 0;
    std::int64_t highest = 0;
    std::int64_t count = 0;
    TokenMap assigned;
    std::vector<std::int64_t> free;
    TokenMap retained;
};
std::optional<VenueEntry> venue_entry(const std::string& as_of, const std::string& mic, bool any_version = false);
std::set<std::string> venues_with_manifest(const std::string& as_of);
Json venue_run(const std::string& as_of, const std::string& mic);

std::string utc_now();

struct Artifact {
    std::string path;
    std::int64_t bytes = 0;
    std::string sha256;
    std::int64_t rows = 0;
    Json as_dict() const;
};
Artifact artifact(const fs::path& path, const std::string& as_of, std::int64_t rows = 0);

struct RunStats {
    std::map<std::string, std::int64_t> day;
    std::string carried_from;
    std::int64_t drawn = 0;
    std::int64_t sequence_before = 0;
    std::int64_t sequence_after = 0;
    std::string anchored_on;
    std::string sequence_from;
    Json as_dict() const;
};

std::map<std::string, std::int64_t> day_stats(const VenueTokens* previous_day, const VenueTokens& tokens,
                                              std::int64_t previous_issued);
RunStats run_stats(const std::string& as_of, const std::string& mic, std::int64_t venue_id,
                   const VenueTokens& tokens, const Sequence* sequence, const std::string& anchored_on = "",
                   const std::string& sequence_from = "");

Json header_payload(const std::string& as_of, const std::string& mic, const VenueTokens& tokens,
                    const std::string& alloc_name, std::int64_t alloc_bytes, const std::string& alloc_sha,
                    const std::string& started_at = "", const RunStats* run = nullptr,
                    const std::vector<Artifact>& inputs = {}, const std::vector<Artifact>& outputs = {},
                    const Json* numbering = nullptr);
fs::path write_header(const std::string& as_of, const std::string& mic, const Json& payload);
fs::path write_venue_manifest(const std::string& as_of, const std::string& mic, const VenueTokens& tokens,
                              const std::string& started_at = "", const RunStats* run = nullptr,
                              const std::vector<Artifact>& inputs = {}, const std::vector<Artifact>& outputs = {},
                              const Json* numbering = nullptr);

struct ExceptionRow {
    std::string script;
    std::int64_t token = 0;
    std::int64_t wanted = 0;
    std::string wanted_from;
    std::string lost_to;
};
fs::path write_exceptions(const fs::path& path, std::vector<ExceptionRow> rows);
std::vector<ExceptionRow> read_exceptions(const std::string& as_of, const std::string& mic);

std::optional<std::int64_t> load_sequence(const std::string& as_of);
std::pair<std::optional<std::int64_t>, std::string> previous_sequence(const std::string& as_of);
std::vector<std::string> numbered_day_dirs();
std::pair<std::int64_t, std::string> highest_issued();
std::pair<Sequence, std::string> open_sequence(const std::string& as_of);
fs::path write_sequence(const std::string& as_of, const Sequence& sequence);

std::pair<std::optional<VenueTokens>, std::string> opening_tokens(const std::string& as_of, const std::string& mic,
                                                                  std::int64_t venue_id);
std::pair<std::optional<VenueTokens>, std::string> previous_tokens(const std::string& as_of, const std::string& mic,
                                                                   std::int64_t venue_id);

const config::Venue* exchange_for(const std::string& venue);
inline void reset_exchange_cache() {}

}  // namespace premarket::counter_token
