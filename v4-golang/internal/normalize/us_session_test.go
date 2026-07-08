package normalize

import (
	"testing"
	"time"
)

func TestTradingSessionXNAS_EDT(t *testing.T) {
	ref := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	got := tradingSessionForXNAS(ref)
	want := "0800-1330|1330-2000|2000-0000"
	if got != want {
		t.Fatalf("XNAS EDT: got %q want %q", got, want)
	}
}

func TestTradingSessionXNAS_EST(t *testing.T) {
	ref := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	got := tradingSessionForXNAS(ref)
	want := "0900-1430|1430-2100|2100-0100"
	if got != want {
		t.Fatalf("XNAS EST: got %q want %q", got, want)
	}
}

func TestTradingSessionOPRAEquity_EDT(t *testing.T) {
	ref := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	got := tradingSessionForOPRA("NVDA", ref)
	want := "1130-1325|1330-2000|2000-2015"
	if got != want {
		t.Fatalf("XCBO equity EDT: got %q want %q", got, want)
	}
}

func TestTradingSessionOPRAIndex_EDT(t *testing.T) {
	ref := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	got := tradingSessionForOPRA("SPXW", ref)
	want := "0015-1325|1330-2015|2015-2100"
	if got != want {
		t.Fatalf("XCBO index EDT: got %q want %q", got, want)
	}
}

func TestTradingSessionGLBX_EDT(t *testing.T) {
	ref := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	got := tradingSessionForGLBX(ref)
	want := "2200-1330|1330-2015|2015-2100"
	if got != want {
		t.Fatalf("XCME Globex EDT: got %q want %q", got, want)
	}
}

func TestEtWindowWrap(t *testing.T) {
	w := etWindow{20, 15, 9, 25}
	ref := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	got := w.toUTCSlot(ref, usEasternLoc())
	if got != "0015-1325" {
		t.Fatalf("wrap: got %q", got)
	}
}
