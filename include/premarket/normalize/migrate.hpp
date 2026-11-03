#pragma once

#include <string>

namespace premarket::migrate {

inline constexpr int FROM_HEAD_VERSION = 1;

int migrate_state(const std::string& reason, bool dry_run);

}  // namespace premarket::migrate
