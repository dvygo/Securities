package normalize

import (
	"regexp"
	"strconv"
	"strings"
	"time"
)

var (
	glbxCPStrike = regexp.MustCompile(`\s+([CP])(\d+(?:\.\d+)?)\s*$`)
	opraOCCTail  = regexp.MustCompile(`(\d{6})([CP])(\d{8})\s*$`)
	rootWeekday  = regexp.MustCompile(`^E([1-5])([A-D])$`)
	rootEW       = regexp.MustCompile(`^EW([1-4])?$`)
	esQuarterly  = regexp.MustCompile(`^ES([HMUZ])(\d)`)
)

var cmeMonth = map[byte]int{
	'F': 1, 'G': 2, 'H': 3, 'J': 4, 'K': 5, 'M': 6,
	'N': 7, 'Q': 8, 'U': 9, 'V': 10, 'X': 11, 'Z': 12,
}

var weekdayLetter = map[byte]int{'A': 0, 'B': 1, 'C': 2, 'D': 3}

func underlyingRootFromStypeIn(stypeIn string) string {
	s := strings.ToUpper(strings.TrimSpace(stypeIn))
	s = strings.TrimSuffix(s, ".OPT")
	s = strings.TrimSuffix(s, ".FUT")
	return s
}

func glbxStrikeInt(stypeOut string, scale int) *int64 {
	m := glbxCPStrike.FindStringSubmatch(strings.TrimSpace(stypeOut))
	if m == nil {
		return nil
	}
	f, err := strconv.ParseFloat(m[2], 64)
	if err != nil {
		return nil
	}
	v := int64(f * float64(scale))
	return &v
}

func nextWeekdayOnOrAfter(asOf time.Time, wd time.Weekday) time.Time {
	d := int(wd - asOf.Weekday())
	if d < 0 {
		d += 7
	}
	return asOf.AddDate(0, 0, d)
}

func glbxExpirationYYYYMMDD(root string, asOf time.Time, stypeOut string) *int64 {
	r := strings.ToUpper(strings.TrimSpace(root))
	if r == "" {
		return nil
	}
	if m := rootWeekday.FindStringSubmatch(r); m != nil {
		letter := m[2][0]
		wd, ok := weekdayLetter[letter]
		if !ok {
			return nil
		}
		exp := nextWeekdayOnOrAfter(asOf, time.Weekday(wd))
		v := int64(exp.Year()*10000 + int(exp.Month())*100 + exp.Day())
		return &v
	}
	if rootEW.MatchString(r) {
		exp := nextWeekdayOnOrAfter(asOf, time.Friday)
		v := int64(exp.Year()*10000 + int(exp.Month())*100 + exp.Day())
		return &v
	}
	sym := strings.ToUpper(strings.TrimSpace(stypeOut))
	token := sym
	if i := strings.IndexByte(sym, ' '); i >= 0 {
		token = sym[:i]
	}
	qm := esQuarterly.FindStringSubmatch(token)
	if qm == nil && r == "ES" {
		qm = esQuarterly.FindStringSubmatch(strings.ReplaceAll(sym, " ", ""))
	}
	if qm != nil {
		month := cmeMonth[qm[1][0]]
		yi, _ := strconv.Atoi(qm[2])
		year := 2000 + yi
		if yi >= 70 {
			year = 1900 + yi
		}
		if month > 0 {
			d := time.Date(year, time.Month(month), 1, 0, 0, 0, 0, time.UTC)
			for d.Weekday() != time.Friday {
				d = d.AddDate(0, 0, 1)
			}
			for d.Month() == time.Month(month) {
				d = d.AddDate(0, 0, 7)
			}
			d = d.AddDate(0, 0, -7)
			v := int64(d.Year()*10000 + int(d.Month())*100 + d.Day())
			return &v
		}
	}
	return nil
}

func parseOPRAOCC(symbol string) (underlying string, exp *int64, strikeThousandths *int64) {
	s := strings.TrimSpace(symbol)
	m := opraOCCTail.FindStringSubmatch(s)
	if m == nil {
		return "", nil, nil
	}
	yymmdd := m[1]
	prefix := s[:len(s)-len(m[0])]
	und := strings.ToUpper(strings.ReplaceAll(prefix, " ", ""))
	if und == "" {
		und = strings.ToUpper(strings.TrimSpace(prefix))
	}
	expInt := yymmddToYYYYMMDD(yymmdd)
	if expInt != nil {
		exp = expInt
	}
	st, err := strconv.ParseInt(m[3], 10, 64)
	if err == nil {
		strikeThousandths = &st
	}
	return und, exp, strikeThousandths
}

func yymmddToYYYYMMDD(yymmdd string) *int64 {
	if len(yymmdd) != 6 {
		return nil
	}
	yi, err1 := strconv.Atoi(yymmdd[:2])
	mo, err2 := strconv.Atoi(yymmdd[2:4])
	da, err3 := strconv.Atoi(yymmdd[4:6])
	if err1 != nil || err2 != nil || err3 != nil {
		return nil
	}
	year := 2000 + yi
	if yi >= 70 {
		year = 1900 + yi
	}
	v := int64(year*10000 + mo*100 + da)
	return &v
}
