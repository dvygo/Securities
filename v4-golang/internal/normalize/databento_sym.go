package normalize

import (
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

func yyyymmddToExpirationNs(yyyymmdd *int64) string {
	if yyyymmdd == nil || *yyyymmdd <= 0 {
		return ""
	}
	s := fmt.Sprintf("%08d", *yyyymmdd)
	t, err := time.Parse("20060102", s)
	if err != nil {
		return ""
	}
	t = time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	return strconv.FormatInt(t.UnixNano(), 10)
}

func symbologyStartTs(row map[string]string) uint64 {
	s := strings.TrimSpace(row["start_ts"])
	if s == "" {
		return 0
	}
	n, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

func symbologyInstrumentID(row map[string]string) int64 {
	s := strings.TrimSpace(row["instrument_id"])
	if s == "" {
		return 0
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// dedupeSymbologyRows keeps one row per stype_out_symbol with the latest start_ts.
func dedupeSymbologyRows(rows []map[string]string) []map[string]string {
	best := make(map[string]map[string]string)
	bestStart := make(map[string]uint64)
	bestID := make(map[string]int64)

	for _, row := range rows {
		key := strings.TrimSpace(row["stype_out_symbol"])
		if key == "" {
			continue
		}
		start := symbologyStartTs(row)
		id := symbologyInstrumentID(row)
		prev, ok := bestStart[key]
		if !ok || start > prev || (start == prev && id > bestID[key]) {
			best[key] = row
			bestStart[key] = start
			bestID[key] = id
		}
	}

	out := make([]map[string]string, 0, len(best))
	for _, row := range best {
		out = append(out, row)
	}
	return out
}

func opraInstType(underlying string) (string, string) {
	u := strings.ToUpper(strings.TrimSpace(underlying))
	if u == "SPXW" || u == "SPX" || u == "VIX" || u == "RUT" {
		return "OPTIDX", "OPTION"
	}
	return "OPTSTK", "OPTION"
}

func glbxInstType(stypeOut string) (string, string) {
	if glbxStrikeInt(stypeOut, IndiaPriceScale) != nil {
		return "OPTIDX", "OPTION"
	}
	stOut := strings.ToUpper(strings.TrimSpace(stypeOut))
	if strings.Contains(stOut, " ") && (strings.Contains(stOut, " C") || strings.Contains(stOut, " P")) {
		return "OPTIDX", "OPTION"
	}
	return "FUTIDX", "FUTURE"
}

func mapDatabentoRow(cols map[string]string) []string {
	out := make([]string, len(paths.NormalizedColumns))
	for i, name := range paths.NormalizedColumns {
		out[i] = cols[name]
	}
	return out
}
