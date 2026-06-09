package normalize

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStageNSECopy(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.csv")
	dst := filepath.Join(dir, "dst.csv")
	if err := os.WriteFile(src, []byte("A,B\n1,2\n3,\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	n, err := stageNSECopy(src, dst, false)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("rows=%d want 2", n)
	}
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	want := "A,B\n1,2\n3,\n"
	if string(got) != want {
		t.Fatalf("got %q want %q", string(got), want)
	}
}
