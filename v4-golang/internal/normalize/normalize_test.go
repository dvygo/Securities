package normalize

import (
	"testing"
)

func TestTradingSessionISTToUTC_NSE(t *testing.T) {
	got, ok := TradingSessionISTToUTC("0915-1530|1815-1915:")
	if !ok || got != "0345-1000|1245-1345" {
		t.Fatalf("got %q ok=%v", got, ok)
	}
}

func TestTradingSessionISTToUTC_MCX(t *testing.T) {
	got, ok := TradingSessionISTToUTC("0900-2330|1815-1915:")
	if !ok || got != "0330-1800|1245-1345" {
		t.Fatalf("got %q ok=%v", got, ok)
	}
}

func TestScalePrice(t *testing.T) {
	cases := map[float64]int64{
		0.01:   1000,
		0.0025: 250,
		1.0:    100000,
		110.0:  11000000,
	}
	for price, want := range cases {
		if got := ScalePrice(price, IndiaPriceScale); got != want {
			t.Fatalf("price %v: got %d want %d", price, got, want)
		}
	}
}

func TestMapFyersRow_CM(t *testing.T) {
	row := map[string]string{
		"symDetails":      "20 MICRONS LTD",
		"exInstType":      "0",
		"minLotSize":      "1",
		"tickSize":        "0.01",
		"isin":            "INE144J01027",
		"tradingSession":  "0915-1530|1815-1915:",
		"expiryDate":      "",
		"symTicker":       "NSE:20MICRONS-EQ",
		"exToken":         "16921",
		"exSymName":       "20MICRONS",
		"strikePrice":     "-1.0",
		"optType":         "XX",
	}
	out, err := mapFyersRow(row)
	if err != nil {
		t.Fatal(err)
	}
	if out[0] != "20 MICRONS LTD" || out[1] != "EQ" || out[2] != "SPOT" || out[3] != "100000" || out[4] != "1" || out[5] != "1000" {
		t.Fatalf("details/type/type2/mult/lot/tick: %v", out[:6])
	}
	if out[8] != "" || out[13] != "" || out[14] != "" {
		t.Fatalf("expiration/strike/optionType should be empty: %v", out[8:])
	}
	if out[9] != "NSE:20MICRONS-EQ" || out[11] != "20MICRONS" || out[12] != "20MICRONS" {
		t.Fatalf("script/underlying_root/underlying: %v", out[9:])
	}
}

func TestMapFyersRow_FOOption(t *testing.T) {
	row := map[string]string{
		"symDetails":     "IOC 28 Jul 26 110 PE",
		"exInstType":     "15",
		"minLotSize":     "4875",
		"tickSize":       "0.01",
		"tradingSession": "0915-1530|1815-1915:",
		"expiryDate":     "1785232800",
		"symTicker":      "NSE:IOC26JUL110PE",
		"exToken":        "106366",
		"exSymName":      "IOC",
		"strikePrice":    "110.0",
		"optType":        "PE",
	}
	out, err := mapFyersRow(row)
	if err != nil {
		t.Fatal(err)
	}
	if out[1] != "OPTSTK" || out[2] != "OPTION" || out[3] != "100000" || out[11] != "IOC" || out[12] != "IOC" || out[13] != "11000000" || out[14] != "PUT" {
		t.Fatalf("got %v", out)
	}
	wantExp := "1785232800000000000"
	if out[8] != wantExp {
		t.Fatalf("expiration: got %q want %q", out[8], wantExp)
	}
}

func TestMapFyersRow_CD(t *testing.T) {
	row := map[string]string{
		"symDetails":     "EURINR 12 Jun 26 FUT",
		"exInstType":     "16",
		"minLotSize":     "1",
		"tickSize":       "0.0025",
		"tradingSession": "0900-1700|1815-1915:",
		"expiryDate":     "1781247600",
		"symTicker":      "NSE:EURINR26612FUT",
		"exToken":        "15432",
		"exSymName":      "EURINR",
		"strikePrice":    "-1.0",
		"optType":        "XX",
	}
	out, err := mapFyersRow(row)
	if err != nil {
		t.Fatal(err)
	}
	if out[1] != "FUTCUR" || out[2] != "" || out[3] != "100000" || out[5] != "250" || out[13] != "" {
		t.Fatalf("got %v", out)
	}
}

func TestMapFyersRow_MCX(t *testing.T) {
	row := map[string]string{
		"symDetails":     "MCXBULLDEX 24 Jun 26 FUT",
		"exInstType":     "11",
		"minLotSize":     "1",
		"tickSize":       "1.0",
		"tradingSession": "0900-2330|1815-1915:",
		"expiryDate":     "1782324000",
		"symTicker":      "MCX:MCXBULLDEX26JUNFUT",
		"exToken":        "565898",
		"exSymName":      "MCXBULLDEX",
		"strikePrice":    "-1.0",
		"optType":        "XX",
	}
	out, err := mapFyersRow(row)
	if err != nil {
		t.Fatal(err)
	}
	if out[1] != "FUTIDX" || out[2] != "FUTURE" || out[3] != "100000" || out[5] != "100000" {
		t.Fatalf("got %v", out)
	}
}

func TestInstrumentType2(t *testing.T) {
	cases := map[string]string{
		"EQ":     "SPOT",
		"FUTSTK": "FUTURE",
		"FUTIDX": "FUTURE",
		"OPTSTK": "OPTION",
		"OPTIDX": "OPTION",
		"FUTCUR": "",
		"":       "",
	}
	for in, want := range cases {
		if got := instrumentType2(in); got != want {
			t.Fatalf("%q: got %q want %q", in, got, want)
		}
	}
}
