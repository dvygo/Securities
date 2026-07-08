package postgres

import (
	"bytes"
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/dvygo/premarket/v4g/internal/baskets"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

// PushJob is one symbology or basket table load.
type PushJob struct {
	Table  string
	CSV    string
	Layout string // fyers, nse, databento, basket
}

// ListSymPushJobs returns symbology CSV jobs under dayDir/normalized/.
func ListSymPushJobs(dayDir string, skipMissing bool) ([]PushJob, error) {
	jobs, err := symJobs(dayDir, skipMissing)
	if err != nil {
		return nil, err
	}
	out := make([]PushJob, len(jobs))
	for i, j := range jobs {
		out[i] = PushJob{j.table, j.csv, j.layout}
	}
	return out, nil
}

// ListBasketPushJobs returns basket contract CSV jobs under contractsDir/.
func ListBasketPushJobs(contractsDir string, skipMissing bool) ([]PushJob, error) {
	if st, err := os.Stat(contractsDir); err != nil || !st.IsDir() {
		if skipMissing {
			fmt.Fprintf(os.Stderr, "skip (missing contracts dir): %s\n", contractsDir)
			return nil, nil
		}
		return nil, fmt.Errorf("not found: %s", contractsDir)
	}
	var jobs []PushJob
	for _, name := range baskets.AllBasketNames {
		p := filepath.Join(contractsDir, name+".csv")
		if _, err := os.Stat(p); err != nil {
			if skipMissing {
				fmt.Fprintf(os.Stderr, "skip (missing): %s\n", p)
				continue
			}
			return nil, fmt.Errorf("missing %s", p)
		}
		jobs = append(jobs, PushJob{name, name + ".csv", "basket"})
	}
	if len(jobs) == 0 && !skipMissing {
		return nil, fmt.Errorf("no basket tables to load")
	}
	return jobs, nil
}

// ExchangeMICForNormalizedCSV maps a normalized output filename to exchange MIC.
func ExchangeMICForNormalizedCSV(name string) string {
	if b, err := paths.FyersMICForOutputCSV(name); err == nil {
		return b.ExchangeMIC
	}
	switch name {
	case paths.XCMECSV:
		return "XCME"
	case paths.XCBOCSV:
		return "XCBO"
	case paths.XNASCSV:
		return "XNAS"
	}
	for _, seg := range paths.NSESegments {
		if seg.OutputCSV == name {
			return seg.ExchangeMIC
		}
	}
	return ""
}

// SymInsertRows returns column names and rows for sqlite/CSV export (postgres-equivalent filtering).
func SymInsertRows(layout, csvPath string) ([]string, [][]string, error) {
	switch layout {
	case "nse":
		header, raw, err := readCSVHeaderAndBody(csvPath)
		if err != nil {
			return nil, nil, err
		}
		if len(header) == 0 || len(bytes.TrimSpace(raw)) == 0 {
			return nil, nil, nil
		}
		rows, err := parseCSVRecords(raw)
		if err != nil {
			return nil, nil, err
		}
		if len(rows) <= 1 {
			return header, nil, nil
		}
		return header, rows[1:], nil
	case "databento":
		data, _, _, _, err := csvBytesForCopyWithRowOK(csvPath, rowOKDatabento)
		if err != nil {
			return nil, nil, err
		}
		return parseInsertBody(colNames, data)
	default:
		data, _, _, _, err := csvBytesForCopy(csvPath)
		if err != nil {
			return nil, nil, err
		}
		return parseInsertBody(colNames, data)
	}
}

// BasketInsertRows returns contract columns and rows (postgres-equivalent).
func BasketInsertRows(csvPath string) ([]string, [][]string, error) {
	data, err := contractCSVBytesForCopy(csvPath)
	if err != nil {
		return nil, nil, err
	}
	return parseInsertBody(contractColNames, data)
}

func parseInsertBody(cols []string, data []byte) ([]string, [][]string, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return cols, nil, nil
	}
	rows, err := parseCSVRecords(data)
	if err != nil {
		return nil, nil, err
	}
	if len(rows) <= 1 {
		return cols, nil, nil
	}
	return cols, rows[1:], nil
}

func parseCSVRecords(data []byte) ([][]string, error) {
	r := csv.NewReader(bytes.NewReader(data))
	var out [][]string
	for {
		rec, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		out = append(out, rec)
	}
	return out, nil
}

// ContractRowFromNormalizedWithLayout builds contract rows using explicit layout.
func ContractRowFromNormalizedWithLayout(date, exchange, layout, csvPath string) ([][]string, error) {
	cols, rows, err := SymInsertRows(layout, csvPath)
	if err != nil {
		return nil, err
	}
	_ = cols
	if len(rows) == 0 {
		return nil, nil
	}
	out := make([][]string, 0, len(rows))
	for _, row := range rows {
		rec := []string{date, exchange}
		rec = append(rec, row...)
		out = append(out, rec)
	}
	return out, nil
}

// NormalizedHeaderOK reports whether a CSV header matches the 16-column symbology layout.
func NormalizedHeaderOK(header []string) bool {
	if len(header) != len(paths.NormalizedColumns) {
		return false
	}
	for i, c := range paths.NormalizedColumns {
		if strings.TrimSpace(header[i]) != c {
			return false
		}
	}
	return true
}
