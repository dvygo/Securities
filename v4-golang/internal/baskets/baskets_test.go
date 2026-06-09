package baskets

import (
	"testing"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

func TestToContractRow_passesNormalizedColumns(t *testing.T) {
	row := map[string]string{
		"scriptDetails":        "IOC 28 Jul 26 110 PE",
		"scriptInstrumentType":  "OPTSTK",
		"scriptInstrumentType2": "OPTION",
		"multiplier":            "100000",
		"lotSize":              "4875",
		"tickSize":             "1000",
		"ISIN":                 "",
		"tradingSessionUTC":    "0345-1000|1245-1345",
		"expiration":           "1785232800000000000",
		"script":               "NSE:IOC26JUL110PE",
		"scriptToken":          "106366",
		"underlying_root":      "IOC",
		"underlying":           "IOC",
		"strike":               "11000000",
		"optionType":           "PUT",
	}
	asOf := time.Date(2026, 6, 9, 0, 0, 0, 0, time.UTC)
	out := toContractRow(row, asOf, "XNSE")
	if len(out) != len(paths.ContractColumns) {
		t.Fatalf("got %d cols want %d", len(out), len(paths.ContractColumns))
	}
	if out[0] != "20260609" || out[1] != "XNSE" || out[2] != row["scriptDetails"] || out[4] != "OPTION" || out[5] != "100000" {
		t.Fatalf("metadata/type2/mult: %v", out[:7])
	}
	if out[len(out)-1] != "PUT" {
		t.Fatalf("optionType: %q", out[len(out)-1])
	}
}

func TestIsFutureRow(t *testing.T) {
	if !isFutureRow(map[string]string{"scriptInstrumentType": "FUTIDX", "script": "NSE:NIFTY26JUNFUT"}) {
		t.Fatal("FUTIDX should be future")
	}
	if isFutureRow(map[string]string{"scriptInstrumentType": "EQ", "script": "NSE:SBIN-EQ"}) {
		t.Fatal("EQ should not be future")
	}
}
