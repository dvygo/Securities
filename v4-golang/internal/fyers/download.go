package fyers

import (
	"bytes"
	"encoding/csv"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

func DownloadSegment(segKey string, asOf time.Time, inputPath string, dryRun bool) (string, error) {
	seg, err := paths.FyersSegmentByKey(segKey)
	if err != nil {
		return "", err
	}
	cfg, err := config.LoadFyers()
	if err != nil {
		return "", err
	}
	out := paths.RawCSV(asOf, seg.OutputCSV)

	if dryRun {
		if inputPath != "" {
			fmt.Fprintf(os.Stderr, "dry-run: %s <- %s -> %s\n", seg.Key, inputPath, out)
		} else {
			fmt.Fprintf(os.Stderr, "dry-run: %s <- %s/%s -> %s\n", seg.Key, strings.TrimRight(cfg.BaseURL, "/"), seg.SourceFile, out)
		}
		return out, nil
	}

	var data []byte
	if inputPath != "" {
		src := inputPath
		if !filepath.IsAbs(src) {
			src = filepath.Join(paths.RepoRoot(), src)
		}
		data, err = os.ReadFile(src)
		if err != nil {
			return "", err
		}
	} else {
		url := strings.TrimRight(cfg.BaseURL, "/") + "/" + seg.SourceFile
		fmt.Fprintf(os.Stderr, "download: %s\n", url)
		data, err = fetchWithRetry(url, cfg)
		if err != nil {
			return "", err
		}
	}

	rows, err := parseHeaderlessCSV(data)
	if err != nil {
		return "", fmt.Errorf("%s: %w", seg.Key, err)
	}
	if err := writeHeaderedCSV(out, rows); err != nil {
		return "", err
	}
	fmt.Fprintf(os.Stderr, "wrote %d rows -> %s\n", len(rows), out)
	return out, nil
}

func DownloadAll(asOf time.Time, inputPath string, dryRun bool) error {
	for _, seg := range paths.FyersSegments {
		if _, err := DownloadSegment(seg.Key, asOf, inputPath, dryRun); err != nil {
			return fmt.Errorf("%s: %w", seg.Key, err)
		}
	}
	return nil
}

func fetchWithRetry(url string, cfg config.Fyers) ([]byte, error) {
	client := &http.Client{Timeout: time.Duration(cfg.TimeoutSec * float64(time.Second))}
	var lastErr error
	for attempt := 1; attempt <= cfg.Retries; attempt++ {
		req, err := http.NewRequest(http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", cfg.UserAgent)
		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
		} else {
			body, readErr := io.ReadAll(resp.Body)
			resp.Body.Close()
			if readErr != nil {
				lastErr = readErr
			} else if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return body, nil
			} else {
				lastErr = fmt.Errorf("HTTP %d", resp.StatusCode)
			}
		}
		if attempt < cfg.Retries {
			fmt.Fprintf(os.Stderr, "retry %d/%d for %s: %v\n", attempt, cfg.Retries, url, lastErr)
			time.Sleep(time.Duration(cfg.RetryDelaySec * float64(time.Second)))
		}
	}
	return nil, fmt.Errorf("download failed for %s: %w", url, lastErr)
}

func parseHeaderlessCSV(data []byte) ([][]string, error) {
	text := strings.TrimPrefix(string(data), "\ufeff")
	ncols := len(paths.FyersRawColumns)
	legacy := ncols - 4
	var rows [][]string
	r := csv.NewReader(strings.NewReader(text))
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	for {
		row, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if len(row) == 0 || (len(row) == 1 && strings.TrimSpace(row[0]) == "") {
			continue
		}
		if len(row) == legacy {
			row = append(row, "", "", "", "")
		} else if len(row) != ncols {
			preview := row
			if len(preview) > 5 {
				preview = preview[:5]
			}
			return nil, fmt.Errorf("expected %d or %d fields, got %d: %v", legacy, ncols, len(row), preview)
		}
		rows = append(rows, row)
	}
	return rows, nil
}

func writeHeaderedCSV(path string, rows [][]string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".fyers_*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	w := csv.NewWriter(tmp)
	if err := w.Write(paths.FyersRawColumns); err != nil {
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

// ReadRawCSV reads a headered Fyers raw CSV into row maps.
func ReadRawCSV(path string) ([]map[string]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(f)
	if err != nil {
		return nil, err
	}
	data = bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
	cr := csv.NewReader(bytes.NewReader(data))
	header, err := cr.Read()
	if err != nil {
		return nil, err
	}
	var rows []map[string]string
	for {
		rec, err := cr.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
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
