package fyers

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadRawCSVHeaderless(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "raw.csv")
	body := "101000000016921,20 MICRONS LTD,0,1,0.01,INE144J01027,0915-1530|1815-1915:,2026-06-08,,NSE:20MICRONS-EQ,10,10,16921,20MICRONS,16921,-1.0,XX,101000000016921,None,1,2.0\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	rows, err := ReadRawCSV(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("rows: got %d want 1", len(rows))
	}
	if rows[0]["fyToken"] != "101000000016921" {
		t.Fatalf("fyToken: %q", rows[0]["fyToken"])
	}
	if rows[0]["symTicker"] != "NSE:20MICRONS-EQ" {
		t.Fatalf("symTicker: %q", rows[0]["symTicker"])
	}
	if rows[0]["exToken"] != "16921" {
		t.Fatalf("exToken: %q", rows[0]["exToken"])
	}
}

func TestReadRawCSVLegacyHeader(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "raw.csv")
	header := "fytoken,symbol,instrumentType,lotSize,tickSize,ISIN,tradingSession,lastUpdate,expiryDate,symbolTicker,exchange,segment,scripCode,scripName,scripToken,strikePrice,optionType,underFyToken,underExSymbol,fyersExtra1,fyersExtra2\n"
	row := "101000000016921,20 MICRONS LTD,0,1,0.01,INE144J01027,0915-1530|1815-1915:,2026-06-08,,NSE:20MICRONS-EQ,10,10,16921,20MICRONS,16921,-1.0,XX,101000000016921,None,1,2.0\n"
	if err := os.WriteFile(path, []byte(header+row), 0o644); err != nil {
		t.Fatal(err)
	}
	rows, err := ReadRawCSV(path)
	if err != nil {
		t.Fatal(err)
	}
	if rows[0]["exSymName"] != "20MICRONS" {
		t.Fatalf("exSymName: %q", rows[0]["exSymName"])
	}
}

func TestWriteRawCSVWithHeader(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "raw.csv")
	rows := [][]string{{
		"101000000016921", "20 MICRONS LTD", "0", "1", "0.01", "INE144J01027",
		"0915-1530|1815-1915:", "2026-06-08", "", "NSE:20MICRONS-EQ", "10", "10",
		"16921", "20MICRONS", "16921", "-1.0", "XX", "101000000016921", "None", "1", "2.0",
	}}
	if err := writeRawCSV(path, rows, true); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data[:7]) != "fyToken" {
		t.Fatalf("expected header, got %q", string(data[:40]))
	}
	parsed, err := ReadRawCSV(path)
	if err != nil || len(parsed) != 1 {
		t.Fatalf("read back: %v len=%d", err, len(parsed))
	}
}
