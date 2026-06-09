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

type DownloadOpts struct {
	AsOf             time.Time
	InputPath        string
	DryRun           bool
	IncludeCSVHeader bool
}

func DownloadSegment(segKey string, opts DownloadOpts) (string, error) {
	seg, err := paths.FyersRawSegmentByKey(segKey)
	if err != nil {
		return "", err
	}
	cfg, err := config.LoadFyers()
	if err != nil {
		return "", err
	}
	out := paths.FyersRawCSV(opts.AsOf, seg.SourceFile)

	if opts.DryRun {
		mode := "headerless"
		if opts.IncludeCSVHeader {
			mode = "with CSV header"
		}
		if opts.InputPath != "" {
			fmt.Fprintf(os.Stderr, "dry-run: %s <- %s -> %s (%s)\n", seg.Key, opts.InputPath, out, mode)
		} else {
			fmt.Fprintf(os.Stderr, "dry-run: %s <- %s/%s -> %s (%s)\n", seg.Key, strings.TrimRight(cfg.BaseURL, "/"), seg.SourceFile, out, mode)
		}
		return out, nil
	}

	var data []byte
	if opts.InputPath != "" {
		src := opts.InputPath
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

	rows, err := parseFyersCSV(data)
	if err != nil {
		return "", fmt.Errorf("%s: %w", seg.Key, err)
	}
	if err := writeRawCSV(out, rows, opts.IncludeCSVHeader); err != nil {
		return "", err
	}
	fmt.Fprintf(os.Stderr, "wrote %d rows -> %s\n", len(rows), out)
	return out, nil
}

func DownloadAll(opts DownloadOpts) error {
	for _, seg := range paths.FyersRawSegments {
		if _, err := DownloadSegment(seg.Key, opts); err != nil {
			return fmt.Errorf("%s: %w", seg.Key, err)
		}
	}
	return nil
}

func fetchWithRetry(url string, cfg config.Fyers) ([]byte, error) {
	// Match curl defaults: HTTP/1.1, User-Agent + Accept only (no Accept-Encoding: gzip).
	transport := &http.Transport{
		ForceAttemptHTTP2: false,
		DisableCompression: true,
	}
	client := &http.Client{
		Timeout:   time.Duration(cfg.TimeoutSec * float64(time.Second)),
		Transport: transport,
	}
	var lastErr error
	for attempt := 1; attempt <= cfg.Retries; attempt++ {
		req, err := http.NewRequest(http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		setCurlHeaders(req, cfg.UserAgent)

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

// setCurlHeaders mirrors curl's default request headers (see curl -v).
func setCurlHeaders(req *http.Request, userAgent string) {
	req.Header = http.Header{}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "*/*")
}

func parseFyersCSV(data []byte) ([][]string, error) {
	text := strings.TrimPrefix(string(data), "\ufeff")
	ncols := columnCount()
	legacy := legacyColumnCount()
	var rows [][]string
	r := csv.NewReader(strings.NewReader(text))
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	first := true
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
		if first && isHeaderRow(row) {
			first = false
			continue
		}
		first = false
		row = padLegacyRow(row)
		if len(row) != ncols {
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

func writeRawCSV(path string, rows [][]string, includeHeader bool) error {
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
	if includeHeader {
		if err := w.Write(JSONColumns); err != nil {
			tmp.Close()
			return err
		}
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

// ReadRawCSV reads a Fyers raw CSV (headerless or headered) into row maps keyed by JSONColumns.
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
	cr.FieldsPerRecord = -1
	cr.LazyQuotes = true

	first, err := cr.Read()
	if err != nil {
		return nil, err
	}
	if len(first) == 0 || (len(first) == 1 && strings.TrimSpace(first[0]) == "") {
		return nil, nil
	}

	var rows []map[string]string
	if isHeaderRow(first) {
		header := first
		for {
			rec, err := cr.Read()
			if err == io.EOF {
				break
			}
			if err != nil {
				return nil, err
			}
			if len(rec) == 0 || (len(rec) == 1 && strings.TrimSpace(rec[0]) == "") {
				continue
			}
			rows = append(rows, rowFromHeadered(header, padLegacyRow(rec)))
		}
		return rows, nil
	}

	rows = append(rows, rowFromFields(first))
	for {
		rec, err := cr.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if len(rec) == 0 || (len(rec) == 1 && strings.TrimSpace(rec[0]) == "") {
			continue
		}
		rec = padLegacyRow(rec)
		if len(rec) != columnCount() {
			return nil, fmt.Errorf("expected %d fields, got %d", columnCount(), len(rec))
		}
		rows = append(rows, rowFromFields(rec))
	}
	return rows, nil
}
