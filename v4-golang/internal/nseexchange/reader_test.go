package nseexchange

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadCSV_sample(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sample.csv")
	if err := os.WriteFile(path, []byte("FinInstrmId,TckrSymb\n1,SBIN\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rows, err := ReadCSV(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0][ColFinInstrmID] != "1" || rows[0][ColTckrSymb] != "SBIN" {
		t.Fatalf("got %+v", rows)
	}
}
