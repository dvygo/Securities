"""Price scaling: convert decimal prices to fixed-point integers."""
from typing import Union

# India price scale: 1 rupee = 100000 units (paise * 1000)
INDIA_PRICE_SCALE = 100000


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
