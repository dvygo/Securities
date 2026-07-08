package databento

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	dbn "github.com/NimbleMarkets/dbn-go"
	dbn_hist "github.com/NimbleMarkets/dbn-go/hist"
)

func rowsFromSymbologyResolution(res *dbn_hist.Resolution, stypeIn dbn.SType, inSymbol string, maxMaps int) []MappingRow {
	if res == nil || len(res.Mappings) == 0 {
		return nil
	}

	stInStr := strconv.Itoa(int(stypeIn))
	stOutStr := strconv.Itoa(int(dbn.SType_RawSymbol))

	var rows []MappingRow
	for rawOut, intervals := range res.Mappings {
		rawOut = cleanDBNString([]byte(rawOut))
		if rawOut == "" {
			continue
		}
		for _, iv := range intervals {
			iid, err := strconv.ParseInt(iv.Symbol, 10, 64)
			if err != nil || iid <= 0 {
				continue
			}
			inSym := inSymbol
			if inSym == "" {
				inSym = rawOut
			}
			row := MappingRow{
				InstrumentID:   iid,
				StypeInSymbol:  inSym,
				StypeOutSymbol: rawOut,
				StypeIn:        stInStr,
				StypeOut:       stOutStr,
				StartTs:        formatDBNTimestamp(symbologyDateToNs(iv.StartDate)),
				EndTs:          formatDBNTimestamp(symbologyDateToNs(iv.EndDate)),
			}
			rows = append(rows, row)
			if maxMaps > 0 && len(rows) >= maxMaps {
				return rows
			}
		}
	}
	return rows
}

func symbologyDateToNs(dateStr string) uint64 {
	dateStr = cleanDBNString([]byte(dateStr))
	if dateStr == "" {
		return 0
	}
	t, err := time.Parse("2006-01-02", dateStr)
	if err != nil {
		t, err = time.Parse(time.RFC3339, dateStr)
		if err != nil {
			return 0
		}
	}
	t = time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	return uint64(t.UnixNano())
}

func logSymbologyResolutionNotes(res *dbn_hist.Resolution) {
	if res == nil {
		return
	}
	if len(res.Partial) > 0 {
		fmt.Fprintf(os.Stderr, "note: symbology.resolve partial for %d symbols\n", len(res.Partial))
	}
	if len(res.NotFound) > 0 {
		fmt.Fprintf(os.Stderr, "warning: symbology.resolve not_found (%d): %v\n", len(res.NotFound), res.NotFound)
	}
	if msg := strings.TrimSpace(res.Message); msg != "" {
		fmt.Fprintf(os.Stderr, "note: symbology.resolve: %s\n", msg)
	}
}
