package databento

import (
	"testing"

	dbn "github.com/NimbleMarkets/dbn-go"
	dbn_hist "github.com/NimbleMarkets/dbn-go/hist"
)

func TestRowsFromSymbologyResolution(t *testing.T) {
	res := &dbn_hist.Resolution{
		Mappings: map[string][]dbn_hist.MappingInterval{
			"NVDA  260731C00240000": {{
				StartDate: "2026-06-24",
				EndDate:   "2026-06-25",
				Symbol:    "889206195",
			}},
		},
	}
	rows := rowsFromSymbologyResolution(res, dbn.SType_Parent, "NVDA.OPT", 0)
	if len(rows) != 1 {
		t.Fatalf("rows=%d", len(rows))
	}
	r := rows[0]
	if r.InstrumentID != 889206195 {
		t.Fatalf("id=%d", r.InstrumentID)
	}
	if r.StypeInSymbol != "NVDA.OPT" || r.StypeOutSymbol != "NVDA  260731C00240000" {
		t.Fatalf("symbols in=%q out=%q", r.StypeInSymbol, r.StypeOutSymbol)
	}
	if r.StartTs == "" || r.EndTs == "" {
		t.Fatalf("timestamps start=%q end=%q", r.StartTs, r.EndTs)
	}
	if r.StartTs == r.EndTs {
		t.Fatal("start/end should differ")
	}
}

func TestSymbologyDateToNs(t *testing.T) {
	got := symbologyDateToNs("2026-06-24")
	if got == 0 {
		t.Fatal("zero")
	}
}
