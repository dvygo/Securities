#pragma once

#include <string>
#include <vector>

namespace premarket::commands {

int load(const std::string& date_dir, const std::string& venue, const std::vector<std::string>& sinks, bool dry_run);
int check_tokens(const std::vector<std::string>& dates, const std::string& venue);
int check_lineage(const std::vector<std::string>& dates, const std::string& venue);
int check_state();
int init_state(const std::string& reason, bool dry_run);
int migrate_state(const std::string& reason, bool dry_run);
int quality_gate(const std::vector<std::string>& dates, const std::string& venue, const std::string& parent_run);

}  // namespace premarket::commands
