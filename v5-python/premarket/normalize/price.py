"""Price scaling: convert decimal prices to fixed-point integers."""
from typing import Union

# India price scale: feed is quoted in paise, so 1 rupee = 100 units. This is
# also what gets written into the "multiplier" column for every Fyers row
# (matching the US convention: multiplier is the wire price scale, so
# strike / multiplier always recovers the real price regardless of venue).
INDIA_PRICE_SCALE = 100


def scale_price(price: Union[float, str], scale: int = INDIA_PRICE_SCALE) -> int:
    """
    Scale decimal price to fixed-point integer.
    E.g., 123.45 with scale 100000 -> 12345000 (paise).
    """
    if isinstance(price, str):
        try:
            price = float(price)
        except ValueError:
            return 0

    if price is None:
        return 0

    return round(price * scale)
