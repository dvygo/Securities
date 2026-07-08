package normalize

import (
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

var (
	opraStripTail = regexp.MustCompile(`(\d{6})([CP])(\d{8})\s*$`)
	rawSymCols    = []string{
		"instrument_id", "stype_in_symbol", "stype_out_symbol",
		"stype_in", "stype_out", "start_ts", "end_ts",
	}
)

func RunStrip(asOf time.Time, dryRun bool) error {
	src := paths.DatabentoRawCSV(asOf, paths.XCBOCSV)
	dst := paths.DatabentoRawCSV(asOf, "XCBO-DATABENTO.stripped.csv")
	fmt.Fprintf(os.Stderr, "strip (xcbo): as_of=%s src=%s\n", asOf.Format("2006-01-02"), src)

	rows, err := readMappingCSV(src)
	if err != nil {
		return err
	}
	if len(rows) == 0 {
		fmt.Fprintf(os.Stderr, "skip (empty): %s\n", src)
		return nil
	}

	filtered := filterOPRANearTerm(rows, asOf)
	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would write %d rows -> %s\n", len(filtered), dst)
		return nil
	}
	if err := writeRawSymbologyCSV(dst, filtered); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "stripped %d -> %d rows -> %s\n", len(rows), len(filtered), dst)
	return nil
}

func normalizeDatabentoFile(src, dst string, fn func(map[string]string, time.Time, usCfg) ([]string, bool), asOf time.Time, cfg usCfg, dryRun bool) error {
	rows, err := readMappingCSV(src)
	if err != nil {
		return err
	}
	if len(rows) == 0 {
		return nil
	}
	before := len(rows)
	rows = dedupeSymbologyRows(rows)
	if before != len(rows) {
		fmt.Fprintf(os.Stderr, "dedupe: %d symbology rows -> %d unique stype_out_symbol (%s)\n", before, len(rows), filepath.Base(src))
	}

	out := make([][]string, 0, len(rows))
	for _, row := range rows {
		mapped, ok := fn(row, asOf, cfg)
		if !ok {
			continue
		}
		out = append(out, mapped)
	}
	if len(out) == 0 {
		fmt.Fprintf(os.Stderr, "skip (empty after map): %s\n", src)
		return nil
	}
	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would write %d rows -> %s\n", len(out), dst)
		return nil
	}
	if err := writeNormalized(dst, out); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "normalized %d rows -> %s\n", len(out), dst)
	return nil
}

func readMappingCSV(path string) ([]map[string]string, error) {
	if _, err := os.Stat(path); err != nil {
		fmt.Fprintf(os.Stderr, "skip (missing): %s\n", path)
		return nil, nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	header, err := r.Read()
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	for i, h := range header {
		header[i] = strings.TrimPrefix(h, "\ufeff")
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
		row := make(map[string]string, len(header))
		for i, h := range header {
			if i < len(rec) {
				row[h] = rec[i]
			}
		}
		rows = append(rows, row)
	}
	return rows, nil
}

func writeRawSymbologyCSV(path string, rows []map[string]string) error {
	if err := os.MkdirAll(filepathDir(path), 0o755); err != nil {
		return err
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	w := csv.NewWriter(f)
	if err := w.Write(rawSymCols); err != nil {
		return err
	}
	for _, row := range rows {
		rec := make([]string, len(rawSymCols))
		for i, c := range rawSymCols {
			rec[i] = row[c]
		}
		if err := w.Write(rec); err != nil {
			return err
		}
	}
	w.Flush()
	return w.Error()
}

func filepathDir(path string) string {
	i := strings.LastIndexAny(path, `/\`)
	if i < 0 {
		return "."
	}
	return path[:i]
}

func filterOPRANearTerm(rows []map[string]string, asOf time.Time) []map[string]string {
	weekly := weeklyExpiryDates(asOf)
	seen := make(map[string]struct{})
	var out []map[string]string
	for _, row := range rows {
		sym := strings.TrimSpace(row["stype_out_symbol"])
		und, exp := parseOPRAOccDate(sym)
		if !keepOPRARow(und, exp, asOf, weekly) {
			continue
		}
		if _, ok := seen[sym]; ok {
			continue
		}
		seen[sym] = struct{}{}
		out = append(out, row)
	}
	return out
}

func parseOPRAOccDate(symbol string) (string, *time.Time) {
	s := strings.TrimSpace(symbol)
	m := opraStripTail.FindStringSubmatch(s)
	if m == nil {
		return "", nil
	}
	prefix := s[:len(s)-len(m[0])]
	und := strings.ToUpper(strings.ReplaceAll(prefix, " ", ""))
	exp := yymmddToDate(m[1])
	return und, exp
}

func yymmddToDate(yymmdd string) *time.Time {
	v := yymmddToYYYYMMDD(yymmdd)
	if v == nil {
		return nil
	}
	s := fmt.Sprintf("%08d", *v)
	t, err := time.Parse("20060102", s)
	if err != nil {
		return nil
	}
	return &t
}

func weeklyExpiryDates(asOf time.Time) map[time.Time]struct{} {
	wd := int(asOf.Weekday())
	daysToFri := (4 - wd + 7) % 7
	f1 := asOf.AddDate(0, 0, daysToFri)
	f2 := f1.AddDate(0, 0, 7)
	return map[time.Time]struct{}{
		truncateDate(f1): {},
		truncateDate(f2): {},
	}
}

func truncateDate(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
}

func keepOPRARow(underlying string, exp *time.Time, asOf time.Time, weekly map[time.Time]struct{}) bool {
	if exp == nil {
		return false
	}
	u := strings.ToUpper(strings.TrimSpace(underlying))
	if u == "SPXW" {
		end := asOf.AddDate(0, 0, 14)
		return !exp.Before(truncateDate(asOf)) && !exp.After(truncateDate(end))
	}
	_, ok := weekly[truncateDate(*exp)]
	return ok
}
