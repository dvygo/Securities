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

	"github.com/dvygo/premarket/v4g/internal/fyers"
	"github.com/dvygo/premarket/v4g/internal/normalize"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

var (
	futTail = regexp.MustCompile(`^[^:]+:(.+?)(\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC))FUT$`)
	eqTail  = regexp.MustCompile(`^[^:]+:(.+)-EQ$`)
)

var outputColumns = []string{
	"date", "exchange", "underlying", "instrument", "expiration",
	"strike", "multiplier", "exToken", "exSymbol", "displaySymbol",
}

var allBasketNames = []string{
	"NIFTY_FNO_EQUITY_SPOTS",
	"NIFTY_FNO_FUTURES_NEAR",
	"NIFTY_FNO_FUTURES_ALL",
	"NSE_INDEX_FUTURES",
	"BSE_INDEX_FUTURES",
	"MCX_FUTURES",
	"ALL_INDEX_FUTURES",
}

type Stats struct {
	Written         int
	DroppedMissing  int
	SkippedNoFut    int
}

type SymIndex struct {
	BySymbol            map[string]map[string]string
	FuturesByUnderlying map[string][]map[string]string
	DisplayBySymbol     map[string]string
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

func parseEQUnderlying(ticker string) string {
	m := eqTail.FindStringSubmatch(strings.TrimSpace(ticker))
	if len(m) < 2 {
		return ""
	}
	return strings.ToUpper(m[1])
}

func parseFutRoot(ticker string) string {
	m := futTail.FindStringSubmatch(strings.TrimSpace(ticker))
	if len(m) < 2 {
		return ""
	}
	return strings.ToUpper(m[1])
}

func intField(row map[string]string, key string) int {
	raw := strings.TrimSpace(row[key])
	if raw == "" {
		return 0
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return 0
	}
	return int(v)
}

func isFutRow(row map[string]string) bool {
	return strings.HasSuffix(strings.ToUpper(strings.TrimSpace(row["symbol"])), "FUT")
}

func loadDisplayNames(rawPath string) map[string]string {
	out := make(map[string]string)
	rows, err := fyers.ReadRawCSV(rawPath)
	if err != nil {
		return out
	}
	for _, row := range rows {
		ticker := strings.TrimSpace(row["symbolTicker"])
		label := strings.TrimSpace(row["symbol"])
		if ticker != "" && label != "" {
			out[ticker] = label
		}
	}
	return out
}

func loadSymbology(normPath, rawPath string) (*SymIndex, error) {
	idx := &SymIndex{
		BySymbol:            make(map[string]map[string]string),
		FuturesByUnderlying: make(map[string][]map[string]string),
		DisplayBySymbol:     loadDisplayNames(rawPath),
	}
	rows, err := normalize.ReadNormalized(normPath)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		sym := strings.TrimSpace(row["symbol"])
		if sym != "" {
			idx.BySymbol[sym] = row
		}
		if isFutRow(row) {
			und := strings.ToUpper(strings.TrimSpace(row["underlying"]))
			if und != "" {
				idx.FuturesByUnderlying[und] = append(idx.FuturesByUnderlying[und], row)
			}
		}
	}
	return idx, nil
}

func inferInstrument(row map[string]string) string {
	sym := strings.ToUpper(strings.TrimSpace(row["symbol"]))
	if strings.HasSuffix(sym, "-EQ") {
		return "SPOT"
	}
	if strings.HasSuffix(sym, "FUT") {
		return "FUT"
	}
	if intField(row, "expiration") == 0 {
		return "SPOT"
	}
	return "FUT"
}

func toContractRow(row map[string]string, asOf time.Time, display map[string]string) []string {
	strikeRaw := strings.TrimSpace(row["strike"])
	exp := intField(row, "expiration")
	sym := strings.TrimSpace(row["symbol"])
	displaySym := ""
	if display != nil {
		displaySym = display[sym]
	}
	strikeOut := strikeRaw
	if strikeOut == "" {
		if exp == 0 {
			strikeOut = "0"
		}
	}
	return []string{
		asOf.Format("20060102"),
		strings.TrimSpace(row["exchange"]),
		strings.TrimSpace(row["underlying"]),
		inferInstrument(row),
		strconv.Itoa(exp),
		strikeOut,
		strconv.Itoa(intField(row, "multiplier")),
		strings.TrimSpace(row["token"]),
		sym,
		displaySym,
	}
}

func liveFutures(idx *SymIndex, underlying string, asOfInt int) []map[string]string {
	rows := idx.FuturesByUnderlying[strings.ToUpper(underlying)]
	var live []map[string]string
	for _, row := range rows {
		exp := intField(row, "expiration")
		if exp >= asOfInt {
			live = append(live, row)
		}
	}
	return live
}

func pickNear(rows []map[string]string) map[string]string {
	if len(rows) == 0 {
		return nil
	}
	best := rows[0]
	bestExp := intField(best, "expiration")
	for _, r := range rows[1:] {
		e := intField(r, "expiration")
		if e < bestExp {
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
	if err := w.Write(outputColumns); err != nil {
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

func normRaw(asOf time.Time, csvName string) (string, string) {
	day := paths.DayDir(asOf)
	return filepath.Join(day, paths.NormalizedSubdir, csvName),
		filepath.Join(day, paths.RawSubdir, csvName)
}

func RefreshBasket(name string, asOf time.Time, dryRun bool) (Stats, error) {
	basketsDir := paths.BasketsDir()
	outPath := filepath.Join(paths.ContractsDayDir(asOf), name+".csv")
	spotsPath := filepath.Join(basketsDir, "NIFTY_FNO_EQUITY_SPOTS.csv")

	switch name {
	case "NIFTY_FNO_EQUITY_SPOTS":
		norm, raw := normRaw(asOf, paths.XNSECSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveSpots(spotsPath, idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY_FNO_FUTURES_NEAR":
		norm, raw := normRaw(asOf, paths.XNFOCSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveStockFuts(spotsPath, idx, asOf, true)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NIFTY_FNO_FUTURES_ALL":
		norm, raw := normRaw(asOf, paths.XNFOCSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveStockFuts(spotsPath, idx, asOf, false)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "NSE_INDEX_FUTURES":
		norm, raw := normRaw(asOf, paths.XNFOCSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveIndexFutsNear(filepath.Join(basketsDir, "NSE_INDEX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "BSE_INDEX_FUTURES":
		norm, raw := normRaw(asOf, paths.XBFOCSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveIndexFutsNear(filepath.Join(basketsDir, "BSE_INDEX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "MCX_FUTURES":
		norm, raw := normRaw(asOf, paths.XMCXCSV)
		idx, err := loadSymbology(norm, raw)
		if err != nil {
			return Stats{}, err
		}
		rows, st := resolveMCXFutsAll(filepath.Join(basketsDir, "MCX_FUTURES.csv"), idx, asOf)
		return st, writeContractCSV(outPath, rows, dryRun)

	case "ALL_INDEX_FUTURES":
		nfoNorm, nfoRaw := normRaw(asOf, paths.XNFOCSV)
		xbfoNorm, xbfoRaw := normRaw(asOf, paths.XBFOCSV)
		mcxNorm, mcxRaw := normRaw(asOf, paths.XMCXCSV)
		nfo, err := loadSymbology(nfoNorm, nfoRaw)
		if err != nil {
			return Stats{}, err
		}
		xbfo, err := loadSymbology(xbfoNorm, xbfoRaw)
		if err != nil {
			return Stats{}, err
		}
		mcx, err := loadSymbology(mcxNorm, mcxRaw)
		if err != nil {
			return Stats{}, err
		}
		nseRows, nseSt := resolveIndexFutsNear(filepath.Join(basketsDir, "NSE_INDEX_FUTURES.csv"), nfo, asOf)
		bseRows, bseSt := resolveIndexFutsNear(filepath.Join(basketsDir, "BSE_INDEX_FUTURES.csv"), xbfo, asOf)
		mcxRows, mcxSt := resolveMCXFutsAll(filepath.Join(basketsDir, "MCX_FUTURES.csv"), mcx, asOf)
		all := append(append(nseRows, bseRows...), mcxRows...)
		st := Stats{Written: len(all)}
		st.SkippedNoFut = nseSt.SkippedNoFut + bseSt.SkippedNoFut + mcxSt.SkippedNoFut
		return st, writeContractCSV(outPath, all, dryRun)

	default:
		return Stats{}, fmt.Errorf("unknown basket %q", name)
	}
}

func resolveSpots(spotsBasket string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	tickers, err := loadBasketSymbols(spotsBasket)
	if err != nil {
		return out, st
	}
	for _, ticker := range tickers {
		row, ok := idx.BySymbol[ticker]
		if !ok {
			st.DroppedMissing++
			continue
		}
		out = append(out, toContractRow(row, asOf, idx.DisplayBySymbol))
	}
	st.Written = len(out)
	return out, st
}

func resolveStockFuts(spotsBasket string, idx *SymIndex, asOf time.Time, near bool) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfInt, _ := strconv.Atoi(asOf.Format("20060102"))
	tickers, err := loadBasketSymbols(spotsBasket)
	if err != nil {
		return out, st
	}
	var underlyings []string
	for _, ticker := range tickers {
		if und := parseEQUnderlying(ticker); und != "" {
			underlyings = append(underlyings, und)
		}
	}
	for _, und := range underlyings {
		live := liveFutures(idx, und, asOfInt)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		if near {
			if picked := pickNear(live); picked != nil {
				out = append(out, toContractRow(picked, asOf, idx.DisplayBySymbol))
			}
		} else {
			sort.Slice(live, func(i, j int) bool {
				return intField(live[i], "expiration") < intField(live[j], "expiration")
			})
			for _, row := range live {
				out = append(out, toContractRow(row, asOf, idx.DisplayBySymbol))
			}
		}
	}
	st.Written = len(out)
	return out, st
}

func resolveIndexFutsNear(template string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfInt, _ := strconv.Atoi(asOf.Format("20060102"))
	tickers, err := loadBasketSymbols(template)
	if err != nil {
		return out, st
	}
	seen := make(map[string]struct{})
	var roots []string
	for _, ticker := range tickers {
		root := parseFutRoot(ticker)
		if root == "" {
			continue
		}
		if _, ok := seen[root]; ok {
			continue
		}
		seen[root] = struct{}{}
		roots = append(roots, root)
	}
	for _, root := range roots {
		live := liveFutures(idx, root, asOfInt)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		if picked := pickNear(live); picked != nil {
			out = append(out, toContractRow(picked, asOf, idx.DisplayBySymbol))
		}
	}
	st.Written = len(out)
	return out, st
}

func resolveMCXFutsAll(template string, idx *SymIndex, asOf time.Time) ([][]string, Stats) {
	var st Stats
	var out [][]string
	asOfInt, _ := strconv.Atoi(asOf.Format("20060102"))
	tickers, err := loadBasketSymbols(template)
	if err != nil {
		return out, st
	}
	seen := make(map[string]struct{})
	var roots []string
	for _, ticker := range tickers {
		root := parseFutRoot(ticker)
		if root == "" {
			continue
		}
		if _, ok := seen[root]; ok {
			continue
		}
		seen[root] = struct{}{}
		roots = append(roots, root)
	}
	for _, root := range roots {
		live := liveFutures(idx, root, asOfInt)
		if len(live) == 0 {
			st.SkippedNoFut++
			continue
		}
		sort.Slice(live, func(i, j int) bool {
			return intField(live[i], "expiration") < intField(live[j], "expiration")
		})
		for _, row := range live {
			out = append(out, toContractRow(row, asOf, idx.DisplayBySymbol))
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
