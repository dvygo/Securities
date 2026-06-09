package normalize

import "math"

const IndiaPriceScale = 100000

func ScalePrice(price float64, scale int) int64 {
	if scale <= 0 {
		scale = IndiaPriceScale
	}
	return int64(math.Round(price * float64(scale)))
}
