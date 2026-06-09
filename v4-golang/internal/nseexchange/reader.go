package nseexchange

import (
	"encoding/csv"
	"fmt"
	"io"
	"os"
)

// ReadCSV loads a headered NSE NEW FILE FORMAT CSV into row maps keyed by column name.
func ReadCSV(path string) ([]map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	r.FieldsPerRecord = -1
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
	if len(rows) == 0 {
		return nil, fmt.Errorf("empty file: %s", path)
	}
	return rows, nil
}
