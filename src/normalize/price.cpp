#include "premarket/normalize/price.hpp"

#include <cmath>

#include "premarket/util.hpp"

namespace premarket::price {

std::int64_t scale_price(double price, std::int64_t scale) {
    double scaled = price * static_cast<double>(scale);
    if (std::isnan(scaled)) throw ValueError("cannot convert float NaN to integer");
    if (std::isinf(scaled)) throw ValueError("cannot convert float infinity to integer");
    return util::round_half_even(scaled);
}

std::int64_t scale_price_text(const std::string& price, std::int64_t scale) {
    auto f = util::parse_float(price);
    if (!f) return 0;
    return scale_price(*f, scale);
}

}  // namespace premarket::price
