#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace premarket::row {

enum Col : std::size_t {
    scriptDetails = 0,
    scriptInstrumentType,
    scriptInstrumentType2,
    multiplier,
    lotSize,
    tickSize,
    ISIN,
    tradingSessionUTC,
    expiration,
    script,
    scriptToken,
    underlying_root,
    underlying,
    strike,
    optionType,
    currency,
    brokerScript1,
    brokerScript2,
    brokerScript3,
    brokerScript4,
    counterToken,
    counterTokenV2,
    enable1,
    enable2,
    def_first,
};

using Row = std::vector<std::string>;

std::size_t width();
Row blank();
std::size_t definition_column(std::size_t field_index);
void fill_flags(Row& r);
void fill_unspecified_broker_scripts(Row& r);

}  // namespace premarket::row
