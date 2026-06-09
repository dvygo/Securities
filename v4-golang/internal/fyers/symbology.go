package fyers

// Symbology appendix constants — https://myapi.fyers.in/docsv3#tag/Appendix

// Monthly expiry month codes ({MMM}).
var MonthlyExpiryMonths = []string{
	"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
	"JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
}

// WeeklyExpiryMonthChar maps calendar month (1–12) to {M} in weekly symbology.
var WeeklyExpiryMonthChar = map[int]string{
	1:  "1",
	2:  "2",
	3:  "3",
	4:  "4",
	5:  "5",
	6:  "6",
	7:  "7",
	8:  "8",
	9:  "9",
	10: "O",
	11: "N",
	12: "D",
}

// WeeklyMonthFromChar decodes {M} back to calendar month (1–12).
func WeeklyMonthFromChar(m string) (int, bool) {
	switch m {
	case "1":
		return 1, true
	case "2":
		return 2, true
	case "3":
		return 3, true
	case "4":
		return 4, true
	case "5":
		return 5, true
	case "6":
		return 6, true
	case "7":
		return 7, true
	case "8":
		return 8, true
	case "9":
		return 9, true
	case "O":
		return 10, true
	case "N":
		return 11, true
	case "D":
		return 12, true
	default:
		return 0, false
	}
}
