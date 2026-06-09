package normalize

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

// RunNSE copies unnormalized NSE NEW FILE FORMAT CSVs into normalized/ under
// *-NSE_EXCHANGE.csv names (byte-for-byte including headers).
func RunNSE(asOf time.Time, dryRun bool) error {
	day := paths.DayDir(asOf)
	fmt.Fprintf(os.Stderr, "nse staging: as_of=%s dir=%s (unnormalized copy)\n", asOf.Format("2006-01-02"), day)
	for _, seg := range paths.NSESegments {
		src := paths.NSEExchangeRawCSV(asOf, seg.SourceFile)
		dst := paths.NormalizedCSV(asOf, seg.OutputCSV)
		if _, err := stageNSECopy(src, dst, dryRun); err != nil {
			return err
		}
	}
	return nil
}

func stageNSECopy(src, dst string, dryRun bool) (int, error) {
	if _, err := os.Stat(src); err != nil {
		fmt.Fprintf(os.Stderr, "skip (missing): %s\n", src)
		return 0, nil
	}
	rows, err := countDataRows(src)
	if err != nil {
		return 0, err
	}
	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would copy %d rows -> %s\n", rows, dst)
		return rows, nil
	}
	if err := copyFile(src, dst); err != nil {
		return 0, err
	}
	fmt.Fprintf(os.Stderr, "staged %d rows -> %s (NSE unnormalized)\n", rows, dst)
	return rows, nil
}

func copyFile(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	dir := filepath.Dir(dst)
	tmp, err := os.CreateTemp(dir, ".nse_*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if _, err := io.Copy(tmp, in); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, dst)
}

func countDataRows(path string) (int, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	if len(data) == 0 {
		return 0, nil
	}
	n := 0
	for _, b := range data {
		if b == '\n' {
			n++
		}
	}
	if n == 0 {
		return 0, nil
	}
	return n - 1, nil
}
