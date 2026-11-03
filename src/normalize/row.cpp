#include "premarket/normalize/row.hpp"

#include "premarket/paths.hpp"

namespace premarket::row {

std::size_t width() { return paths::NORMALIZED_COLUMNS.size(); }

Row blank() { return Row(width()); }

std::size_t definition_column(std::size_t field_index) { return def_first + field_index; }

void fill_flags(Row& r) {
    if (r[enable1].empty()) r[enable1] = "0";
    if (r[enable2].empty()) r[enable2] = "0";
}

void fill_unspecified_broker_scripts(Row& r) {
    r[brokerScript2].clear();
    r[brokerScript3].clear();
    r[brokerScript4].clear();
}

}  // namespace premarket::row
