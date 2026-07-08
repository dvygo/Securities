package databento

import (
	"encoding/binary"
	"testing"
	"time"

	dbn "github.com/NimbleMarkets/dbn-go"
)

func TestResolveHistRangeLookback(t *testing.T) {
	first := time.Date(2026, 6, 20, 0, 0, 0, 0, time.UTC)
	lastDay := time.Date(2026, 6, 30, 0, 0, 0, 0, time.UTC)
	end := lastDay.Add(24 * time.Hour)
	start := end.Add(-7 * 24 * time.Hour)

	if !start.Equal(time.Date(2026, 6, 24, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("start=%s", start)
	}
	if start.Before(first) {
		start = first
	}
	if !start.Equal(time.Date(2026, 6, 24, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("clamped start=%s want 2026-06-24", start)
	}
}

func TestDecodeInstrumentDefV1Offsets(t *testing.T) {
	rec := make([]byte, instrumentDefV1Size)
	rec[0] = byte(instrumentDefV1Size / 4)
	copy(rec[instrumentDefV1RawSymbolOff:], []byte("AAPL  260501P00265000\x00"))
	body := rec[dbn.RHeader_Size:]
	binary.LittleEndian.PutUint64(body[24:32], 0x10)
	binary.LittleEndian.PutUint64(body[32:40], 0x20)

	got, err := decodeInstrumentDefV1(rec)
	if err != nil {
		t.Fatal(err)
	}
	if got.RawSymbol != "AAPL  260501P00265000" {
		t.Fatalf("raw_symbol=%q", got.RawSymbol)
	}
	if got.Expiration != 0x10 {
		t.Fatalf("expiration=%x", got.Expiration)
	}
	if got.Activation != 0x20 {
		t.Fatalf("activation=%x", got.Activation)
	}
}
