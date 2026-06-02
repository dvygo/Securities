package normalize

import (
	"encoding/csv"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/fyers"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

func RunFyers(asOf time.Time, cfg config.Normalizer, dryRun bool) error {
	day := paths.DayDir(asOf)
	fmt.Fprintf(os.Stderr, "normalizer: as_of=%s dir=%s\n", asOf.Format("2006-01-02"), day)
	for _, seg := range paths.FyersSegments {
		src := paths.RawCSV(asOf, seg.OutputCSV)
		dst := paths.NormalizedCSV(asOf, seg.OutputCSV)
		if _, err := rewriteFyers(src, dst, seg, asOf, cfg, dryRun); err != nil {
			return err
		}
	}
	return nil
}

func rewriteFyers(src, dst string, seg paths.FyersSegment, asOf time.Time, cfg config.Normalizer, dryRun bool) (int, error) {
	if _, err := os.Stat(src); err != nil {
		fmt.Fprintf(os.Stderr, "skip (missing): %s\n", src)
		return 0, nil
	}
	rows, err := fyers.ReadRawCSV(src)
	if err != nil {
		return 0, err
	}
	if len(rows) == 0 {
		fmt.Fprintf(os.Stderr, "skip (no header): %s\n", src)
		return 0, nil
	}

	warnings := 0
	out := make([][]string, 0, len(rows))
	dateInt := asOf.Format("20060102")
	exKey := seg.Key + "_exchange"
	exchange := cfg[exKey]
	if exchange == "" {
		exchange = seg.ExchangeMIC
	}

	for _, row := range rows {
		extra := normalizeFyersRow(row, seg, asOf, exchange)
		if missingStrikeExp(extra) && strings.TrimSpace(row["symbol"]) != "" {
			warnings++
		}
		out = append(out, mergeFyersRow(row, extra, dateInt))
	}

	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would write %d rows -> %s\n", len(out), dst)
		return len(out), nil
	}
	if err := writeNormalized(dst, out); err != nil {
		return 0, err
	}
	msg := fmt.Sprintf("normalized %d rows -> %s", len(out), dst)
	if warnings > 0 {
		msg += fmt.Sprintf(" (%d rows missing strike/expiration)", warnings)
	}
	fmt.Fprintln(os.Stderr, msg)
	return len(out), nil
}

func normalizeFyersRow(row map[string]string, seg paths.FyersSegment, asOf time.Time, exchange string) map[string]string {
	root, und := fyersUnderlying(row)
	extra := map[string]string{
		"date":            asOf.Format("20060102"),
		"exchange":        exchange,
		"underlying_root": root,
		"underlying":      und,
		"strike":          "",
		"expiration":      "",
		"multiplier":      strconv.Itoa(fyersLotSize(row, seg.CashMarket)),
	}
	if seg.CashMarket {
		extra["strike"] = "0"
		extra["expiration"] = "0"
	} else {
		if s := fyersStrikePaise(row); s != "" {
			extra["strike"] = s
		}
		if e := fyersExpiryInt(row["expiryDate"]); e != "" {
			extra["expiration"] = e
		}
	}
	return extra
}

func mergeFyersRow(row, extra map[string]string, _ string) []string {
	return []string{
		extra["date"],
		extra["exchange"],
		extra["underlying_root"],
		extra["underlying"],
		extra["strike"],
		extra["expiration"],
		extra["multiplier"],
		strings.TrimSpace(row["scripCode"]),
		strings.TrimSpace(row["symbolTicker"]),
	}
}

func fyersUnderlying(row map[string]string) (string, string) {
	name := strings.ToUpper(strings.TrimSpace(row["scripName"]))
	if name != "" {
		return name, name
	}
	ticker := strings.ToUpper(strings.TrimSpace(row["symbolTicker"]))
	sym := strings.TrimSpace(row["symbol"])
	if ticker != "" {
		if i := strings.Index(ticker, ":"); i >= 0 {
			ticker = ticker[i+1:]
		}
		base := ticker
		if j := strings.Index(base, "-"); j >= 0 {
			base = base[:j]
		}
		return base, base
	}
	if i := strings.Index(sym, ":"); i >= 0 {
		tail := strings.ToUpper(strings.TrimSpace(sym[i+1:]))
		base := tail
		if j := strings.Index(base, "-"); j >= 0 {
			base = base[:j]
		}
		return base, base
	}
	u := strings.ToUpper(sym)
	return u, u
}

func fyersStrikePaise(row map[string]string) string {
	raw := strings.TrimSpace(row["strikePrice"])
	if raw == "" {
		return ""
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v <= 0 {
		return ""
	}
	return strconv.Itoa(int(math.Round(v * 100)))
}

func fyersLotSize(row map[string]string, cash bool) int {
	if cash {
		return 1
	}
	raw := strings.TrimSpace(row["lotSize"])
	if raw == "" {
		return 1
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v <= 0 {
		return 1
	}
	return int(v)
}

func fyersExpiryInt(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" || s == "0" || s == "-1" {
		return ""
	}
	if len(s) == 8 {
		allDigit := true
		for _, c := range s {
			if c < '0' || c > '9' {
				allDigit = false
				break
			}
		}
		if allDigit {
			return s
		}
	}
	ts, err := strconv.ParseFloat(s, 64)
	if err != nil || ts <= 0 {
		return ""
	}
	its := int64(ts)
	if its > 1_000_000_000_000 {
		its /= 1000
	}
	t := time.Unix(its, 0).UTC()
	return t.Format("20060102")
}

func missingStrikeExp(extra map[string]string) bool {
	return extra["strike"] == "" && extra["expiration"] == ""
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

// ReadNormalized loads normalized CSV keyed by symbol column.
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
