#pragma once

#include <cstdint>
#include <optional>
#include <string>

#include "premarket/normalize/row.hpp"
#include "premarket/runner.hpp"
#include "premarket/sources/fyers.hpp"

namespace premarket::fields {

struct Mapped {
    row::Row row;
    std::string exchange;
};

std::string instrument_type2(const std::string& inst_type);
std::string classify_instrument(const std::string& ex_inst_type);
std::optional<std::int64_t> safe_int(const std::string& raw);
std::string expiration_ns(const std::string& raw);
Mapped map_fyers_row(const fyers::Row& raw);
void run(runner::Opts& opts);

}  // namespace premarket::fields
