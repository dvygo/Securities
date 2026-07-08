package databento

import (
	"fmt"
	"os"
	"time"

	dbn "github.com/NimbleMarkets/dbn-go"
	dbn_hist "github.com/NimbleMarkets/dbn-go/hist"
)

type HistOpts struct {
	APIKey       string
	Dataset      string
	Symbols      []string
	StypeIn      dbn.SType
	AsOf         time.Time
	LookbackDays int
	Range        *HistRange
	MaxMaps      int
	InSymbol     string // parent/ticker used for stype_in_symbol on InstrumentDef rows
}

type HistRange struct {
	Start   time.Time // inclusive UTC midnight
	End     time.Time // exclusive UTC midnight
	EndDay  time.Time // last session day included
	First   time.Time
	LastDay time.Time
}

func ResolveHistRange(apiKey, dataset string, asOf time.Time, lookbackDays int) (HistRange, error) {
	dr, err := dbn_hist.GetDatasetRange(apiKey, dataset)
	if err != nil {
		return HistRange{}, fmt.Errorf("metadata.get_dataset_range: %w", err)
	}
	first := dr.Start.UTC()
	lastExclusive := dr.End.UTC()
	lastDay := lastExclusive.Add(-24 * time.Hour)
	if lastDay.Before(first) {
		lastDay = first
	}

	endDay := asOf.UTC()
	if endDay.IsZero() {
		endDay = lastDay
	} else {
		endDay = time.Date(endDay.Year(), endDay.Month(), endDay.Day(), 0, 0, 0, 0, time.UTC)
	}
	if endDay.After(lastDay) {
		endDay = lastDay
	}
	if endDay.Before(first) {
		endDay = first
	}
	if lookbackDays <= 0 {
		lookbackDays = 7
	}

	end := time.Date(endDay.Year(), endDay.Month(), endDay.Day(), 0, 0, 0, 0, time.UTC).Add(24 * time.Hour)
	start := end.Add(-time.Duration(lookbackDays) * 24 * time.Hour)
	if start.Before(first) {
		start = first
	}

	hr := HistRange{
		Start:   start,
		End:     end,
		EndDay:  endDay,
		First:   first,
		LastDay: lastDay,
	}
	return hr, nil
}

func LogHistRange(hr HistRange, asOf time.Time, lookbackDays int) {
	if lookbackDays <= 0 {
		lookbackDays = 7
	}
	if !asOf.IsZero() && hr.EndDay.Format("20060102") != asOf.Format("20060102") {
		fmt.Fprintf(os.Stderr, "note: hist end day clamped to %s (dataset range %s .. %s)\n",
			hr.EndDay.Format("2006-01-02"), hr.First.Format("2006-01-02"), hr.LastDay.Format("2006-01-02"))
	}
	fmt.Fprintf(os.Stderr, "note: hist range %s .. %s (%d-day lookback ending %s)\n",
		hr.Start.Format("2006-01-02"), hr.End.Add(-24*time.Hour).Format("2006-01-02"), lookbackDays, hr.EndDay.Format("2006-01-02"))
}

func HistoricalSymbolMappings(opts HistOpts) ([]MappingRow, error) {
	hr := opts.Range
	if hr == nil {
		r, err := ResolveHistRange(opts.APIKey, opts.Dataset, opts.AsOf, opts.LookbackDays)
		if err != nil {
			return nil, err
		}
		hr = &r
	}

	res, err := dbn_hist.SymbologyResolve(opts.APIKey, dbn_hist.ResolveParams{
		Dataset:   opts.Dataset,
		Symbols:   opts.Symbols,
		StypeIn:   opts.StypeIn,
		StypeOut:  dbn.SType_InstrumentId,
		DateRange: dbn_hist.DateRange{Start: hr.Start, End: hr.End},
	})
	if err != nil {
		return nil, fmt.Errorf("symbology.resolve: %w", err)
	}
	logSymbologyResolutionNotes(res)

	inSym := opts.InSymbol
	if inSym == "" && len(opts.Symbols) == 1 {
		inSym = opts.Symbols[0]
	}
	rows := rowsFromSymbologyResolution(res, opts.StypeIn, inSym, opts.MaxMaps)
	if len(rows) == 0 {
		return nil, fmt.Errorf("symbology.resolve returned no mappings")
	}
	return rows, nil
}
