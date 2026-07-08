package databento

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
)

func appendMappingCSV(path string, rows []MappingRow) error {
	if len(rows) == 0 {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	hasBody := false
	if st, err := os.Stat(path); err == nil && st.Size() > 0 {
		hasBody = true
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		r := csv.NewReader(f)
		header, err := r.Read()
		f.Close()
		if err != nil {
			return fmt.Errorf("read header %s: %w", path, err)
		}
		if len(header) != len(MappingColumns) {
			return fmt.Errorf("%s has incompatible header (got %d cols, expected %d)", path, len(header), len(MappingColumns))
		}
	}

	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()

	// UTF-8 BOM on new files (match Python utf-8-sig).
	if !hasBody {
		if _, err := f.Write([]byte{0xEF, 0xBB, 0xBF}); err != nil {
			return err
		}
	}

	w := csv.NewWriter(f)
	if !hasBody {
		if err := w.Write(MappingColumns); err != nil {
			return err
		}
	}
	for _, row := range rows {
		if err := w.Write(row.CSVRecord()); err != nil {
			return err
		}
	}
	w.Flush()
	return w.Error()
}
