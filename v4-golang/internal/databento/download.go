package databento

import (
	"context"
	"fmt"
	"os"
	"time"

	dbn "github.com/NimbleMarkets/dbn-go"

	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

type DownloadOpts struct {
	Venue       Venue
	Mode        Mode
	AsOf        time.Time
	DryRun      bool
	AllSymbols  bool
	SymbolsFile string
	StypeIn     string
	LiveStart   int
}

func Download(opts DownloadOpts) (int, error) {
	symbols, isAll, err := ResolveSymbols(opts.Venue, opts.AllSymbols, opts.SymbolsFile)
	if err != nil {
		return 0, err
	}

	stInStr := opts.StypeIn
	if stInStr == "" {
		stInStr = opts.Venue.DefaultStypeIn(isAll)
	}

	csvPath := paths.DatabentoRawCSV(opts.AsOf, opts.Venue.OutputCSV())
	perSym := opts.Venue.PerSymbolSessions(isAll, opts.SymbolsFile != "")

	fmt.Fprintf(os.Stderr, "databento %s %s: dataset=%s csv=%s symbols=%d per_session=%v stype_in=%s\n",
		opts.Venue, opts.Mode, opts.Venue.Dataset(), csvPath, len(symbols), perSym, stInStr)

	if opts.DryRun {
		fmt.Fprintf(os.Stderr, "dry-run: would fetch symbology -> %s\n", csvPath)
		return 0, nil
	}

	cfg, err := config.LoadDatabento()
	if err != nil {
		return 0, err
	}

	apiKey, err := cfg.APIKeyForES(opts.Venue.UsesESKey())
	if err != nil {
		return 0, err
	}

	stIn, err := dbn.STypeFromString(stInStr)
	if err != nil {
		return 0, fmt.Errorf("stype_in: %w", err)
	}

	total := 0
	var histRange *HistRange
	if opts.Mode == ModeHist {
		hr, err := ResolveHistRange(apiKey, opts.Venue.Dataset(), opts.AsOf, cfg.HistLookbackDays)
		if err != nil {
			return 0, err
		}
		LogHistRange(hr, opts.AsOf, cfg.HistLookbackDays)
		histRange = &hr
	}

	if perSym {
		for i, sym := range symbols {
			fmt.Fprintf(os.Stderr, "  symbol %d/%d: %q\n", i+1, len(symbols), sym)
			n, err := fetchBatch(opts, cfg, apiKey, []string{sym}, stIn, sym, csvPath, histRange)
			if err != nil {
				fmt.Fprintf(os.Stderr, "warning: skip %q: %v\n", sym, err)
				continue
			}
			total += n
		}
	} else {
		n, err := fetchBatch(opts, cfg, apiKey, symbols, stIn, "", csvPath, histRange)
		if err != nil {
			return total, err
		}
		total = n
	}

	fmt.Fprintf(os.Stderr, "wrote/appended %s (+%d rows, total this run %d)\n", csvPath, total, total)
	if total == 0 {
		return 0, fmt.Errorf("no symbol mappings collected")
	}
	return total, nil
}

func fetchBatch(opts DownloadOpts, cfg config.Databento, apiKey string, symbols []string, stIn dbn.SType, inSymbol, csvPath string, histRange *HistRange) (int, error) {
	var rows []MappingRow
	var err error

	switch opts.Mode {
	case ModeLive:
		ctx := context.Background()
		rows, err = LiveSymbolMappings(ctx, LiveOpts{
			APIKey:     apiKey,
			Dataset:    opts.Venue.Dataset(),
			Symbols:    symbols,
			StypeIn:    stIn,
			Seconds:    cfg.LiveSeconds,
			LiveStart:  opts.LiveStart,
			MaxMaps:    cfg.MaxMaps,
			Retries:    cfg.LiveRetries,
			RetryDelay: cfg.LiveRetryDelaySec,
		})
	case ModeHist:
		inSym := inSymbol
		if inSym == "" && len(symbols) == 1 {
			inSym = symbols[0]
		}
		rows, err = HistoricalSymbolMappings(HistOpts{
			APIKey:       apiKey,
			Dataset:      opts.Venue.Dataset(),
			Symbols:      symbols,
			StypeIn:      stIn,
			AsOf:         opts.AsOf,
			LookbackDays: cfg.HistLookbackDays,
			Range:        histRange,
			MaxMaps:      cfg.MaxMaps,
			InSymbol:     inSym,
		})
	default:
		return 0, fmt.Errorf("unknown mode")
	}
	if err != nil && len(rows) == 0 {
		return 0, err
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "warning: partial batch: %v\n", err)
	}

	if err := appendMappingCSV(csvPath, rows); err != nil {
		return 0, err
	}
	fmt.Fprintf(os.Stderr, "  csv +%d rows -> %s\n", len(rows), csvPath)
	return len(rows), nil
}
