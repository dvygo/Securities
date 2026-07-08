package export

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"

	"github.com/dvygo/premarket/v4g/internal/paths"
	"github.com/dvygo/premarket/v4g/internal/postgres"
)

// WriteAggregateCSVs writes contracts.csv (all symbology) and baskets.csv (all basket contracts).
func WriteAggregateCSVs(dayDir, contractsDir, dateStr, outDir string, skipMissing bool) error {
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return err
	}
	contractRows, err := AggregateContractRows(dayDir, dateStr, skipMissing)
	if err != nil {
		return err
	}
	basketRows, err := AggregateBasketRows(contractsDir, skipMissing)
	if err != nil {
		return err
	}
	nContracts, err := writeCSV(filepath.Join(outDir, "contracts.csv"), paths.ContractColumns, contractRows)
	if err != nil {
		return err
	}
	nBaskets, err := writeCSV(filepath.Join(outDir, "baskets.csv"), paths.ContractColumns, basketRows)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "csv-export: %d symbology rows -> %s\n", nContracts, filepath.Join(outDir, "contracts.csv"))
	fmt.Fprintf(os.Stderr, "csv-export: %d basket rows -> %s\n", nBaskets, filepath.Join(outDir, "baskets.csv"))
	return nil
}

// AggregateContractRows merges all v2 symbology normalized CSVs into contract-column rows.
func AggregateContractRows(dayDir, dateStr string, skipMissing bool) ([][]string, error) {
	jobs, err := postgres.ListSymPushJobs(dayDir, skipMissing)
	if err != nil {
		return nil, err
	}
	var all [][]string
	for _, j := range jobs {
		if j.Layout == "nse" {
			fmt.Fprintf(os.Stderr, "skip NSE exchange layout %s (non-v2 columns)\n", j.CSV)
			continue
		}
		exchange := postgres.ExchangeMICForNormalizedCSV(j.CSV)
		if exchange == "" {
			continue
		}
		csvPath := filepath.Join(dayDir, paths.NormalizedSubdir, j.CSV)
		rows, err := postgres.ContractRowFromNormalizedWithLayout(dateStr, exchange, j.Layout, csvPath)
		if err != nil {
			return nil, err
		}
		all = append(all, rows...)
	}
	return all, nil
}

// AggregateBasketRows merges all basket contract CSVs into contract-column rows.
func AggregateBasketRows(contractsDir string, skipMissing bool) ([][]string, error) {
	jobs, err := postgres.ListBasketPushJobs(contractsDir, skipMissing)
	if err != nil {
		return nil, err
	}
	var all [][]string
	for _, j := range jobs {
		csvPath := filepath.Join(contractsDir, j.CSV)
		_, rows, err := postgres.BasketInsertRows(csvPath)
		if err != nil {
			return nil, err
		}
		all = append(all, rows...)
	}
	return all, nil
}

func writeCSV(path string, header []string, rows [][]string) (int, error) {
	f, err := os.Create(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	w := csv.NewWriter(f)
	if err := w.Write(header); err != nil {
		return 0, err
	}
	for _, row := range rows {
		if err := w.Write(row); err != nil {
			return 0, err
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return 0, err
	}
	return len(rows), nil
}
