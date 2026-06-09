package baskets

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/normalize"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

var (
	eqTail  = regexp.MustCompile(`^[^:]+:(.+)-EQ$`)
	futTail = regexp.MustCompile(`^[^:]+:(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))FUT$`)
)

var allBasketNames = []string{
	"NIFTY_FNO_EQUITY_SPOTS",
	"NIFTY_FNO_FUTURES_NEAR",
	"NIFTY_FNO_FUTURES_ALL",
	"NIFTY500_EQUITY_ONLY",
	"NIFTY500_FUTURES",
	"NSE_INDEX_FUTURES",
	"BSE_INDEX_FUTURES",
	"MCX_FUTURES",
	"ALL_INDEX_FUTURES",
}

type Stats struct {
	Written        int
	DroppedMissing int
	SkippedNoFut   int
}

type SymIndex struct {
	ExchangeMIC              string
	ByScript                 map[string]map[string]string
	FuturesByUnderlyingRoot  map[string][]map[string]string
}

func asOfUTCStartNs(t time.Time) int64 {
	d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	return d.UnixNano()
}

func loadBasketSymbols(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, line := range strings.Split(string(data), "\n") {
		s := strings.TrimSpace(line)
		if s == "" || strings.HasPrefix(s, "#") {
			continue
		}
		out = append(out, s)
	}
	return out, nil
}

func parseEQUnderlyingRoot(ticker string) string {
	m := eqTail.FindStringSubmatch(strings.TrimSpace(ticker))
	if len(m) < 2 {
		return ""
	}
	return strings.ToUpper(m[1])
}

func parseFutUnderlyingRoot(ticker string) string {
	m := futTail.FindStringSubmatch(strings.TrimSpace(ticker))
	if len(m) < 2 {
		return ""
	}
	return strings.ToUpper(m[1])
}

func int64Field(row map[string]string, key string) int64 {
	raw := strings.TrimSpace(row[key])
	if raw == "" {
		return 0
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		f, err2 := strconv.ParseFloat(raw, 64)
		if err2 != nil {
			return 0
		}
		return int64(f)
	}
	return v
}

func isFutureRow(row map[string]string) bool {
	t := strings.ToUpper(strings.TrimSpace(row["scriptInstrumentType"]))
	if strings.HasPrefix(t, "FUT") {
		return true
	}
	return strings.HasSuffix(strings.ToUpper(strings.TrimSpace(row["script"])), "FUT")
}

func loadSymIndex(normPath, exchangeMIC string) (*SymIndex, error) {
	idx := &SymIndex{
		ExchangeMIC:             exchangeMIC,
		ByScript:                make(map[string]map[string]string),
		FuturesByUnderlyingRoot: make(map[string][]map[string]string),
	}
	rows, err := normalize.ReadNormalized(normPath)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		script := strings.TrimSpace(row["script"])
		if script != "" {
			idx.ByScript[script] = row
		}
		if isFutureRow(row) {
			root := strings.ToUpper(strings.TrimSpace(row["underlying_root"]))
			if root != "" {
				idx.FuturesByUnderlyingRoot[root] = append(idx.FuturesByUnderlyingRoot[root], row)
			}
		}
	}
	return idx, nil
}

func toContractRow(row map[string]string, asOf time.Time, exchangeMIC string) []string {
	out := make([]string, 0, len(paths.ContractColumns))
	out = append(out, asOf.Format("20060102"), exchangeMIC)
	for _, col := range paths.NormalizedColumns {
		out = append(out, strings.TrimSpace(row[col]))
	}
	return out
}

func liveFutures(idx *SymIndex, underlyingRoot string, asOfNs int64) []map[string]string {
	rows := idx.FuturesByUnderlyingRoot[strings.ToUpper(underlyingRoot)]
	var live []map[string]string
	for _, row := range rows {
		if int64Field(row, "expiration") >= asOfNs {
			live = append(live, row)
		}
	}
	return live
}

func pickNearestExpiry(rows []map[string]string) map[string]string {
	if len(rows) == 0 {
		return nil
	}
	best := rows[0]
	bestExp := int64Field(best, "expiration")
	for _, r := range rows[1:] {
		if e := int64Field(r, "expiration"); e < bestExp {
			best = r
			bestExp = e
		}
	}
	return best
}

func writeContractCSV(path string, rows [][]string, dryRun bool) error {
	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would write %d rows -> %s\n", len(rows), path)
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	w := csv.NewWriter(f)
	if err := w.Write(paths.ContractColumns); err != nil {
		return err
	}
	for _, row := range rows {
		if err := w.Write(row); err != nil {
			return err
		}
	}
	w.Flush()
	fmt.Fprintf(os.Stderr, "wrote %d rows -> %s\n", len(rows), path)
	return w.Error()
}

func segmentNorm(asOf time.Time, outputCSV string) (normPath, exchangeMIC string, err error) {
	bundle, err := paths.FyersMICForOutputCSV(outputCSV)
	if err != nil {
		return "", "", err
	}
	return paths.NormalizedCSV(asOf, outputCSV), bundle.ExchangeMIC, nil
}

func RefreshBasket(name string, asOf time.Time, dryRun bool) (Stats, error) {
	basketsDir := paths.BasketsDir()
	outPath := filepath.Join(paths.ContractsDayDir(asOf), name+".csv")
	spotsBasket := filepath.Join(basketsDir, "NIFTY_FNO_EQUITY_SPOTS.csv")

	switch name {
	case "NIFTY_FNO_EQUITY_SPOTS":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveByScript(spotsBasket, idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY_FNO_FUTURES_NEAR":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveEquityFutures(spotsBasket, idx, asOf, true)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY_FNO_FUTURES_ALL":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveEquityFutures(spotsBasket, idx, asOf, false)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY500_EQUITY_ONLY":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveByScript(filepath.Join(basketsDir, "NIFTY500_EQUITY_ONLY.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY500_FUTURES":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		nifty500Spots := filepath.Join(basketsDir, "NIFTY500_EQUITY_ONLY.csv")
		rows, st := resolveEquityFutures(nifty500Spots, idx, asOf, false)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NSE_INDEX_FUTURES":
		norm, mic, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveIndexFuturesNear(filepath.Join(basketsDir, "NSE_INDEX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "BSE_INDEX_FUTURES":
		norm, mic, err := segmentNorm(asOf, paths.XBOMCSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveIndexFuturesNear(filepath.Join(basketsDir, "BSE_INDEX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "MCX_FUTURES":
		norm, mic, err := segmentNorm(asOf, paths.XIMCCSV)
		if err != nil {
			return Stats{}, err
		}
		idx, err := loadSymIndex(norm, mic)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveIndexFuturesAll(filepath.Join(basketsDir, "MCX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "ALL_INDEX_FUTURES":
		xnseNorm, xnseMIC, err := segmentNorm(asOf, paths.XNSECSV)
		if err != nil {
			return Stats{}, err
		}
		xbomNorm, xbomMIC, err := segmentNorm(asOf, paths.XBOMCSV)
		if err != nil {
			return Stats{}, err
		}
		mcxNorm, mcxMIC, err := segmentNorm(asOf, paths.XIMCCSV)
		if err != nil {
			return Stats{}, err
		}
		xnse, err := loadSymIndex(xnseNorm, xnseMIC)
		if err != nil {
			return Stats{}, err
		}
		xbom, err := loadSymIndex(xbomNorm, xbomMIC)
		if err != nil {
			return Stats{}, err
		}
		mcx, err := loadSymIndex(mcxNorm, mcxMIC)
		if err != nil {
			return Stats{}, err
		}
		nseRows, nseSt := resolveIndexFuturesNear(filepath.Join(basketsDir, "NSE_INDEX_FUTURES.csv"), xnse, asOf)
		bseRows, bseSt := resolveIndexFuturesNear(filepath.Join(basketsDir, "BSE_INDEX_FUTURES.csv"), xbom, asOf)
		mcxRows, mcxSt := resolveIndexFuturesAll(filepath.Join(basketsDir, "MCX_FUTURES.csv"), mcx, asOf)
		all := append(append(nseRows, bseRows...), mcxRows...)
		st := Stats{Written: len(all)}
		st.SkippedNoFut = nseSt.SkippedNoFut + bseSt.SkippedNoFut + mcxSt.SkippedNoFut
		return st, writeContractCSV(outPath, all, dryRun)

	default:
		return Stats{}, fmt.Errorf("unknown basket %q", name)
	}
}

func resolveByScript(template string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	scripts, err := loadBasketSymbols(template)
	if err != nil {
		return out, st
	}
	for _, script := range scripts {
		row, ok := idx.ByScript[script]
		if !ok {
			st.DroppedMissing++
			continue
		}
		out = append(out, toContractRow(row, asOf, idx.ExchangeMIC))
	}
	st.Written = len(out)
	return out, st
}

func resolveEquityFutures(spotsBasket string, idx *SymIndex, asOf time.Time, nearOnly bool) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfNs := asOfUTCStartNs(asOf)
	scripts, err := loadBasketSymbols(spotsBasket)
	if err != nil {
		return out, st
	}
	seen := make(map[string]struct{})
	for _, script := range scripts {
		root := parseEQUnderlyingRoot(script)
		if root == "" {
			continue
		}
		if _, ok := seen[root]; ok {
			continue
		}
		seen[root] = struct{}{}

		live := liveFutures(idx, root, asOfNs)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		if nearOnly {
			if picked := pickNearestExpiry(live); picked != nil {
				out = append(out, toContractRow(picked, asOf, idx.ExchangeMIC))
			}
			continue
		}
		sort.Slice(live, func(i, j int) bool {
			return int64Field(live[i], "expiration") < int64Field(live[j], "expiration")
		})
		for _, row := range live {
			out = append(out, toContractRow(row, asOf, idx.ExchangeMIC))
		}
	}
	st.Written = len(out)
	return out, st
}

func resolveIndexFuturesNear(template string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfNs := asOfUTCStartNs(asOf)
	scripts, err := loadBasketSymbols(template)
	if err != nil {
		return out, st
	}
	seen := make(map[string]struct{})
	for _, script := range scripts {
		root := parseFutUnderlyingRoot(script)
		if root == "" {
			continue
		}
		if _, ok := seen[root]; ok {
			continue
		}
		seen[root] = struct{}{}

		live := liveFutures(idx, root, asOfNs)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		if picked := pickNearestExpiry(live); picked != nil {
			out = append(out, toContractRow(picked, asOf, idx.ExchangeMIC))
		}
	}
	st.Written = len(out)
	return out, st
}

func resolveIndexFuturesAll(template string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfNs := asOfUTCStartNs(asOf)
	scripts, err := loadBasketSymbols(template)
	if err != nil {
		return out, st
	}
	seen := make(map[string]struct{})
	for _, script := range scripts {
		root := parseFutUnderlyingRoot(script)
		if root == "" {
			continue
		}
		if _, ok := seen[root]; ok {
			continue
		}
		seen[root] = struct{}{}

		live := liveFutures(idx, root, asOfNs)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		sort.Slice(live, func(i, j int) bool {
			return int64Field(live[i], "expiration") < int64Field(live[j], "expiration")
		})
		for _, row := range live {
			out = append(out, toContractRow(row, asOf, idx.ExchangeMIC))
		}
	}
	st.Written = len(out)
	return out, st
}

func RefreshAll(asOf time.Time, dryRun bool) error {
	fmt.Fprintf(os.Stderr, "basket_refresh: as_of=%s day=%s\n", asOf.Format("2006-01-02"), paths.DayDir(asOf))
	for _, name := range allBasketNames {
		if name == "ALL_INDEX_FUTURES" {
			continue
		}
		st, err := RefreshBasket(name, asOf, dryRun)
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "%s: written=%d missing=%d no_fut=%d\n",
			name, st.Written, st.DroppedMissing, st.SkippedNoFut)
	}
	st, err := RefreshBasket("ALL_INDEX_FUTURES", asOf, dryRun)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "ALL_INDEX_FUTURES: written=%d no_fut=%d\n", st.Written, st.SkippedNoFut)
	return nil
}

func Run(asOf time.Time, basket string, dryRun bool) error {
	if _, err := os.Stat(paths.NormalizedCSV(asOf, paths.XNSECSV)); err != nil {
		return fmt.Errorf("missing normalized symbology under %s", paths.DayDir(asOf))
	}
	if basket == "" || basket == "all" {
		return RefreshAll(asOf, dryRun)
	}
	st, err := RefreshBasket(basket, asOf, dryRun)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "%s: written=%d missing=%d no_fut=%d\n",
		basket, st.Written, st.DroppedMissing, st.SkippedNoFut)
	return nil
}
