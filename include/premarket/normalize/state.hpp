#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "premarket/normalize/numbering.hpp"
#include "premarket/jsonio.hpp"
#include "premarket/util.hpp"

namespace premarket::state {

namespace fs = std::filesystem;
using Json = jsonio::Json;
using LogFn = std::function<void(const std::string&)>;

inline constexpr const char* STATE_DIR = "_state";
inline constexpr const char* HEAD_NAME = "manifest.json";
inline constexpr int HEAD_VERSION = 8;
inline constexpr const char* STAGED = ".staged";
inline constexpr const char* RUNS_NAME = "runs.jsonl";
inline constexpr int BACKUPS_KEEP = 10;

struct StateMissing : RuntimeError {
    using RuntimeError::RuntimeError;
};
struct StateCorrupt : ValueError {
    using ValueError::ValueError;
};
struct LockHeld : RuntimeError {
    using RuntimeError::RuntimeError;
};

fs::path state_dir();
fs::path head_path();
fs::path snapshot_path(const std::string& relative);
fs::path runs_path();
fs::path backups_dir();
std::string backups_hint();

struct VenueHead {
    Json venue_id = 0;
    std::string snapshot;
    std::string sha256;
    Json rows = 0;
    std::string newest;
    std::string newest_mode;
    std::string live_date;
    std::string run_id;
    std::string updated_at;

    std::int64_t venue_id_int() const;
    std::int64_t rows_int() const;
    Json as_dict() const;
    static VenueHead from_dict(const Json& doc);
};

struct Head {
    std::int64_t counter = 0;
    std::string updated_at;
    Json last_run = Json::object();
    std::map<std::string, VenueHead> venues;
    Json last_commit;
    int version = HEAD_VERSION;

    Json as_dict() const;
    static Head from_dict(const Json& doc);
};

std::optional<Head> read_head();
Head require_head();
fs::path write_head(Head& head);
void atomic_write(const fs::path& path, const std::string& payload);

struct Snapshot {
    std::string relative;
    std::string sha256;
    std::int64_t rows = 0;
};
Snapshot write_snapshot(const std::string& mic, const std::string& run_id, const numbering::Holdings& holdings,
                        const VenueHead* current = nullptr, bool staged = false);
numbering::Holdings load_snapshot(const std::string& mic, const VenueHead& venue);

std::vector<std::string> venue_days(const std::string& mic);
std::optional<numbering::DayAlloc> day_alloc(const std::string& date, const std::string& mic);
std::pair<std::int64_t, std::string> global_counter(const Head* head);

std::string new_run_id(const std::string& date, const std::string& mode);
std::string operator_id();

class Lock {
public:
    explicit Lock(const std::string& command);
    ~Lock();
    Lock(const Lock&) = delete;
    Lock& operator=(const Lock&) = delete;

private:
    int fd_ = -1;
};

void append_event(const Json& event);
std::vector<Json> read_events();
Json event(const std::string& event_type, const std::string& run_id, const std::string& job, const Json& facets,
           const Json& inputs = Json::array(), const Json& outputs = Json::array(), const std::string& parent = "");

std::pair<fs::path, std::vector<std::string>> backup(const std::string& run_id);
std::vector<std::string> recover(const Head& head, const LogFn& log);
std::optional<Head> init_state(const std::string& reason, bool dry_run, const LogFn& log);

bool path_less(const fs::path& a, const fs::path& b);

}  // namespace premarket::state
