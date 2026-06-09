package normalize

import (
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/dvygo/premarket/v4g/internal/fyers"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

func RunFyers(asOf time.Time, dryRun bool) error {
	day := paths.DayDir(asOf)
	fmt.Fprintf(os.Stderr, "normalizer (fyers): as_of=%s dir=%s scale=%d\n", asOf.Format("2006-01-02"), day, IndiaPriceScale)
	for _, bundle := range paths.FyersMICBundles {
		dst := paths.NormalizedCSV(asOf, bundle.OutputCSV)
		if _, err := rewriteFyersBundle(asOf, bundle, dst, dryRun); err != nil {
			return err
		}
	}
	return nil
}

func RunAll(asOf time.Time, dryRun bool) error {
	if err := RunFyers(asOf, dryRun); err != nil {
		return err
	}
	return RunNSE(asOf, dryRun)
}

func rewriteFyersBundle(asOf time.Time, bundle paths.FyersMICBundle, dst string, dryRun bool) (int, error) {
	var out [][]string
	skipped := 0
	for _, sourceFile := range bundle.SourceFiles {
		src := paths.FyersRawCSV(asOf, sourceFile)
		rows, nSkip, err := mapFyersSource(src)
		if err != nil {
			return 0, err
		}
		skipped += nSkip
		out = append(out, rows...)
	}
	if len(out) == 0 {
		fmt.Fprintf(os.Stderr, "skip (empty): %s\n", dst)
		return 0, nil
	}
	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would write %d rows -> %s (%s)\n", len(out), dst, bundle.ExchangeMIC)
		return len(out), nil
	}
	if err := writeNormalized(dst, out); err != nil {
		return 0, err
	}
	msg := fmt.Sprintf("normalized %d rows -> %s (%s)", len(out), dst, bundle.ExchangeMIC)
	if skipped > 0 {
		msg += fmt.Sprintf(" (%d rows skipped)", skipped)
	}
	fmt.Fprintln(os.Stderr, msg)
	return len(out), nil
}

func mapFyersSource(src string) ([][]string, int, error) {
	if _, err := os.Stat(src); err != nil {
		fmt.Fprintf(os.Stderr, "skip (missing): %s\n", src)
		return nil, 0, nil
	}
	rows, err := fyers.ReadRawCSV(src)
	if err != nil {
		return nil, 0, err
	}
	if len(rows) == 0 {
		fmt.Fprintf(os.Stderr, "skip (empty): %s\n", src)
		return nil, 0, nil
	}
	skipped := 0
	out := make([][]string, 0, len(rows))
	for _, row := range rows {
		mapped, err := mapFyersRow(row)
		if err != nil {
			skipped++
			continue
		}
		out = append(out, mapped)
	}
	return out, skipped, nil
}

func writeNormalized(path string, rows [][]string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".norm_*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	w := csv.NewWriter(tmp)
	if err := w.Write(paths.NormalizedColumns); err != nil {
		tmp.Close()
		return err
	}
	for _, row := range rows {
		if err := w.Write(row); err != nil {
			tmp.Close()
			return err
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}

// ReadNormalized loads normalized CSV keyed by column name.
func ReadNormalized(path string) ([]map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	header, err := r.Read()
	if err != nil {
		return nil, err
	}
	var rows []map[string]string
	for {
		rec, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return rows, err
		}
		m := make(map[string]string, len(header))
		for i, h := range header {
			if i < len(rec) {
				m[h] = rec[i]
			}
		}
		rows = append(rows, m)
	}
	return rows, nil
}
