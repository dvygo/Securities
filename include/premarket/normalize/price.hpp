#pragma once

#include <cstdint>
#include <string>

namespace premarket::price {

inline constexpr std::int64_t INDIA_PRICE_SCALE = 100;
inline constexpr std::int64_t US_PRICE_SCALE = 1000000000;

std::int64_t scale_price(double price, std::int64_t scale = INDIA_PRICE_SCALE);
std::int64_t scale_price_text(const std::string& price, std::int64_t scale = INDIA_PRICE_SCALE);

}  // namespace premarket::price
