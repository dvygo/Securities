package normalize

import (
	"testing"
	"time"
)

func TestParseOPRAOCC(t *testing.T) {
	und, exp, strike := parseOPRAOCC("AAPL  260117C00150000")
	if und != "AAPL" {
		t.Fatalf("underlying: %q", und)
	}
	if exp == nil || *exp != 20260117 {
		t.Fatalf("expiration: %v", exp)
	}
	if strike == nil || *strike != 150000 {
		t.Fatalf("strike: %v", strike)
	}
}

func TestMapEQUSRow(t *testing.T) {
	cfg := usCfg{equsExchange: "XNAS"}
	asOf := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	out, ok := mapEQUSRow(map[string]string{
		"instrument_id": "123", "stype_out_symbol": "NVDA",
	}, asOf, cfg)
	if !ok {
		t.Fatal("expected ok")
	}
	if out[9] != "NVDA" || out[10] != "123" {
		t.Fatalf("equs row: %v", out)
	}
	if out[3] != "100000" || out[15] != "USD" {
		t.Fatalf("multiplier/currency: %v", out)
	}
	if out[7] != "0800-1330|1330-2000|2000-0000" {
		t.Fatalf("tradingSessionUTC: %q", out[7])
	}
}

func TestMapOPRARow(t *testing.T) {
	cfg := usCfg{opraExchange: "XCBO", opraMultiplier: 100000}
	asOf := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	out, ok := mapOPRARow(map[string]string{
		"instrument_id":    "889195503",
		"stype_in_symbol":  "NVDA.OPT",
		"stype_out_symbol": "NVDA  260717C00185000",
	}, asOf, cfg)
	if !ok {
		t.Fatal("expected ok")
	}
	if out[10] != "889195503" {
		t.Fatalf("token: %v", out[10])
	}
	if out[9] != "NVDA  260717C00185000" {
		t.Fatalf("script: %v", out[9])
	}
	if out[13] == "" || out[14] != "CALL" {
		t.Fatalf("strike/optionType: %v %v", out[13], out[14])
	}
	if out[7] != "1130-1325|1330-2000|2000-2015" {
		t.Fatalf("tradingSessionUTC: %q", out[7])
	}
}

func TestDedupeSymbologyRows(t *testing.T) {
	rows := []map[string]string{
		{"stype_out_symbol": "NVDA  260710P00280000", "instrument_id": "100", "start_ts": "1000"},
		{"stype_out_symbol": "NVDA  260710P00280000", "instrument_id": "200", "start_ts": "2000"},
		{"stype_out_symbol": "AAPL  260117C00150000", "instrument_id": "300", "start_ts": "500"},
	}
	out := dedupeSymbologyRows(rows)
	if len(out) != 2 {
		t.Fatalf("want 2 rows, got %d", len(out))
	}
	bySym := make(map[string]string)
	for _, r := range out {
		bySym[r["stype_out_symbol"]] = r["instrument_id"]
	}
	if bySym["NVDA  260710P00280000"] != "200" {
		t.Fatalf("expected latest start_ts winner id=200, got %q", bySym["NVDA  260710P00280000"])
	}
}

func TestKeepOPRARowSPXW(t *testing.T) {
	asOf := time.Date(2026, 6, 29, 0, 0, 0, 0, time.UTC)
	exp := asOf.AddDate(0, 0, 7)
	if !keepOPRARow("SPXW", &exp, asOf, nil) {
		t.Fatal("expected SPXW within 14d to be kept")
	}
}

func TestGlbxStrikeInt(t *testing.T) {
	st := glbxStrikeInt("ESM6 C5500", 100000)
	if st == nil || *st != 5500*100000 {
		t.Fatalf("strike: %v", st)
	}
}
