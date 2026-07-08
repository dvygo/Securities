package databento

import (
	"testing"

	dbn "github.com/NimbleMarkets/dbn-go"
)

func TestCleanDBNString(t *testing.T) {
	in := append([]byte{0, 0, 0, 0, 0, 0, 0, 0}, []byte("NVDA  261218P00006000")...)
	got := cleanDBNString(in)
	if got != "NVDA  261218P00006000" {
		t.Fatalf("got %q", got)
	}
}

func TestFormatDBNTimestamp(t *testing.T) {
	if formatDBNTimestamp(0) != "" {
		t.Fatal("zero")
	}
	if formatDBNTimestamp(dbn.UNDEF_TIMESTAMP) != "" {
		t.Fatal("undef")
	}
	if formatDBNTimestamp(123) != "123" {
		t.Fatal("value")
	}
}
