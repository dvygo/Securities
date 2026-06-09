package fyers

import "testing"

func TestResolveExchangeMICFromRow(t *testing.T) {
	row := map[string]string{
		"exchange": "10",
		"segment":  "11",
	}
	mic, ok := ResolveExchangeMICFromRow(row)
	if !ok || mic != "XNFO" {
		t.Fatalf("got %q ok=%v want XNFO", mic, ok)
	}
}

func TestValidExchangeSegment(t *testing.T) {
	if !ValidExchangeSegment(10, 20) {
		t.Fatal("NSE COM should be valid")
	}
	if ValidExchangeSegment(11, 10) {
		t.Fatal("MCX CM should not be valid")
	}
}

func TestInstrumentTypeName(t *testing.T) {
	cases := map[int]string{
		0:  "EQ",
		5:  "SGB",
		9:  "ETF",
		11: "FUTIDX",
		14: "OPTIDX",
		30: "FUTCOM",
		33: "FUTBAS",
		37: "OPTFUT_NCOM",
		50: "MISC_BSE",
	}
	for code, want := range cases {
		got, ok := InstrumentTypeName(code)
		if !ok || got != want {
			t.Fatalf("code %d: got %q ok=%v want %q", code, got, ok, want)
		}
	}
}

func TestParseFyTokenCM(t *testing.T) {
	tok, ok := ParseFyToken("101000000016921")
	if !ok || tok.Exchange != 10 || tok.Segment != 10 || tok.Expiry != "000000" || tok.ExToken != "16921" {
		t.Fatalf("got %+v ok=%v", tok, ok)
	}
	tok, ok = ParseFyToken("10100000004")
	if !ok || tok.ExToken != "4" {
		t.Fatalf("short CM: got %+v ok=%v", tok, ok)
	}
}

func TestParseFyTokenFO(t *testing.T) {
	tok, ok := ParseFyToken("101126063062326")
	if !ok || tok.Exchange != 10 || tok.Segment != 11 || tok.Expiry != "260630" || tok.ExToken != "62326" {
		t.Fatalf("got %+v ok=%v", tok, ok)
	}
	if !tok.HasExpiry() {
		t.Fatal("FO future should have expiry")
	}
}

func TestParseFyTokenMCX(t *testing.T) {
	tok, ok := ParseFyToken("1120260624565898")
	if !ok || tok.Exchange != 11 || tok.Segment != 20 || tok.Expiry != "260624" || tok.ExToken != "565898" {
		t.Fatalf("got %+v ok=%v", tok, ok)
	}
}

func TestIsOptionType(t *testing.T) {
	if !IsOptionType("CE") || !IsOptionType("PE") {
		t.Fatal("expected CE/PE true")
	}
	if IsOptionType("XX") || IsOptionType("") {
		t.Fatal("expected XX/empty false")
	}
}

func TestWeeklyMonthFromChar(t *testing.T) {
	if m, ok := WeeklyMonthFromChar("O"); !ok || m != 10 {
		t.Fatalf("O => 10, got %d ok=%v", m, ok)
	}
	if m, ok := WeeklyMonthFromChar("D"); !ok || m != 12 {
		t.Fatalf("D => 12, got %d ok=%v", m, ok)
	}
}
