package databento

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParentOPT(t *testing.T) {
	if got := parentOPT("aapl"); got != "AAPL.OPT" {
		t.Fatalf("parentOPT: got %q", got)
	}
	if got := parentOPT("SPY.OPT"); got != "SPY.OPT" {
		t.Fatalf("parentOPT suffix: got %q", got)
	}
}

func TestDefaultESParentSymbols(t *testing.T) {
	syms := defaultESParentSymbols()
	if len(syms) < 10 {
		t.Fatalf("expected many ES parents, got %d", len(syms))
	}
	foundESFUT := false
	for _, s := range syms {
		if s == "ES.FUT" {
			foundESFUT = true
		}
		if s == "ES" {
			t.Fatalf("bare ES should become ES.OPT, got %q", s)
		}
	}
	if !foundESFUT {
		t.Fatal("missing ES.FUT in default list")
	}
}

func TestAppendMappingCSV(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.csv")
	rows := []MappingRow{{
		InstrumentID: 1, StypeInSymbol: "AAPL.OPT", StypeOutSymbol: "AAPL  260117C00150000",
		StypeIn: "4", StypeOut: "1",
	}}
	if err := appendMappingCSV(path, rows); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(b) < 3 || b[0] != 0xEF {
		t.Fatalf("expected UTF-8 BOM, got %v", b[:3])
	}
	if err := appendMappingCSV(path, rows); err != nil {
		t.Fatal(err)
	}
}

func TestRowFromInstrumentDef(t *testing.T) {
	// Minimal smoke: empty raw symbol path handled upstream; mapping uses string fields.
	row := MappingRow{
		InstrumentID: 42, StypeInSymbol: "ES.OPT", StypeOutSymbol: "ESH6",
	}
	rec := row.CSVRecord()
	if len(rec) != len(MappingColumns) {
		t.Fatalf("csv cols: got %d want %d", len(rec), len(MappingColumns))
	}
}
