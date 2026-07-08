package databento

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

const allSymbols = "ALL_SYMBOLS"

const esParentRootsCSV = "E1A,E1B,E1C,E1D,E2A,E2B,E2C,E2D,E3A,E3B,E3C,E3D,E4A,E4B,E4C,E4D," +
	"EW1,EW2,EW3,EW4,EW,E5A,E5B,E5C,E5D,ES,ES.FUT,ES.v.0,ES.v.1"

func DefaultUnderlyingsPath() string {
	return filepath.Join(paths.BasketsDir(), "XNAS-XCBOE-underlyings.csv")
}

func defaultESParentSymbols() []string {
	parts := strings.Split(strings.ReplaceAll(esParentRootsCSV, "\n", ""), ",")
	var out []string
	seen := make(map[string]struct{})
	for _, p := range parts {
		s := strings.ToUpper(strings.TrimSpace(p))
		if s == "" {
			continue
		}
		if strings.Contains(s, ".") {
			if _, ok := seen[s]; !ok {
				seen[s] = struct{}{}
				out = append(out, s)
			}
			continue
		}
		opt := s + ".OPT"
		if _, ok := seen[opt]; !ok {
			seen[opt] = struct{}{}
			out = append(out, opt)
		}
	}
	return out
}

func readSymbolLines(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		out = append(out, strings.ToUpper(line))
	}
	return out, sc.Err()
}

func parentOPT(ticker string) string {
	t := strings.ToUpper(strings.TrimSpace(ticker))
	if strings.HasSuffix(t, ".OPT") {
		return t
	}
	return t + ".OPT"
}

func ResolveSymbols(venue Venue, allSym bool, symbolsFile string) ([]string, bool, error) {
	if allSym {
		return []string{allSymbols}, true, nil
	}
	if symbolsFile != "" {
		p := symbolsFile
		if !filepath.IsAbs(p) {
			p = filepath.Join(paths.RepoRoot(), p)
		}
		lines, err := readSymbolLines(p)
		if err != nil {
			return nil, false, fmt.Errorf("symbols file: %w", err)
		}
		if len(lines) == 0 {
			return nil, false, fmt.Errorf("no symbols in %s", p)
		}
		return dedupe(lines), false, nil
	}

	switch venue {
	case VenueXCME:
		return defaultESParentSymbols(), false, nil
	case VenueXCBO, VenueXNAS:
		p := DefaultUnderlyingsPath()
		lines, err := readSymbolLines(p)
		if err != nil {
			return nil, false, fmt.Errorf("underlyings: %w", err)
		}
		if len(lines) == 0 {
			return nil, false, fmt.Errorf("no symbols in %s", p)
		}
		out := make([]string, 0, len(lines))
		for _, line := range dedupe(lines) {
			if venue == VenueXCBO {
				out = append(out, parentOPT(line))
			} else {
				out = append(out, line)
			}
		}
		return out, false, nil
	default:
		return nil, false, fmt.Errorf("unknown venue")
	}
}

func dedupe(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	var out []string
	for _, s := range in {
		if s == "" {
			continue
		}
		if _, ok := seen[s]; ok {
			continue
		}
		seen[s] = struct{}{}
		out = append(out, s)
	}
	return out
}

func joinSymbols(symbols []string) string {
	return strings.Join(symbols, ",")
}
