package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/export"
	"github.com/dvygo/premarket/v4g/internal/paths"
	"github.com/dvygo/premarket/v4g/internal/runlog"
	"github.com/dvygo/premarket/v4g/internal/runner"
	"github.com/dvygo/premarket/v4g/internal/sqlitedb"
)

func main() {
	os.Exit(run())
}

func run() int {
	var (
		onlyStr      string
		dateDir      string
		dryRun       bool
		postgresPush bool
		databaseURL  string
		basket       string
		csvOut       string
		testDB       string
	)

	flag.StringVar(&onlyStr, "only", "all", "steps: all, or comma-separated (normalize,normalize-fyers,normalize-nse,normalize-databento,strip,baskets,postgres)")
	flag.StringVar(&dateDir, "date-dir", "", "YYYYMMDD day folder (default: today)")
	flag.BoolVar(&dryRun, "dry-run", false, "print actions only")
	flag.BoolVar(&postgresPush, "postgres-push", false, "also push Postgres when --only omits postgres (default all run includes postgres)")
	flag.StringVar(&databaseURL, "database-url", "", "override postgres URL")
	flag.StringVar(&basket, "basket", "all", "basket name for --only baskets")
	flag.StringVar(&csvOut, "csv", "", "export aggregated contracts.csv + baskets.csv to this directory")
	flag.StringVar(&testDB, "test-db", "", "load aggregated contracts + baskets tables into SQLite file")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, `normalizer — India symbology normalization (Go)

  normalizer                           # normalize + US databento + baskets + postgres
  normalizer --only normalize-fyers    # Fyers only
  normalizer --only normalize-nse      # NSE NEW FILE FORMAT only
  normalizer --only normalize,baskets   # skip postgres
  normalizer --only postgres            # push v4_YYYYMMDD + v4_YYYYMMDD_baskets only
  normalizer --date-dir 20260702 --csv test
  normalizer --date-dir 20260702 --test-db test/test.db
  normalizer --date-dir 20260702 --only csv-export,test-db --csv test --test-db test/test.db
  normalizer --postgres-push --only normalize,postgres
  normalizer --date-dir 20260609
  normalizer --dry-run

Fyers:  %s/YYYYMMDD/raw/FYERS/
NSE:    %s/YYYYMMDD/raw/NSE_EXCHANGE/NEW FILE FORMAT/

`, paths.RepoRoot(), paths.RepoRoot())
		flag.PrintDefaults()
	}
	flag.Parse()

	asOf := time.Now()
	if dateDir != "" {
		t, err := time.Parse("20060102", dateDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: --date-dir must be YYYYMMDD\n")
			return 2
		}
		asOf = t
	}
	dateDir = asOf.Format("20060102")

	cleanup, logPath, err := runlog.Setup("normalizer", dateDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: log setup: %v\n", err)
		return 2
	}
	defer cleanup()
	if !dryRun {
		fmt.Fprintf(os.Stderr, "log:     %s\n", logPath)
	}

	only := parseOnly(onlyStr)
	exportOnly := len(only) > 0 && onlyExportSteps(only)
	if !exportOnly {
		steps, err := runner.BuildNormalizerSteps(only, postgresPush)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			return 2
		}

		if !dryRun {
			fmt.Fprintf(os.Stderr, "root:     %s\n", paths.RepoRoot())
			fmt.Fprintf(os.Stderr, "config:   %s\n", paths.ConfigINI())
			fmt.Fprintf(os.Stderr, "date-dir: %s\n", dateDir)
		}

		opts := runner.Opts{
			AsOf:        asOf,
			DateDir:     dateDir,
			DryRun:      dryRun,
			DatabaseURL: databaseURL,
			Basket:      basket,
		}

		if err := runner.Run(steps, opts); err != nil {
			fmt.Fprintf(os.Stderr, "error: %v\n", err)
			return 1
		}
	}

	if err := runPostExports(asOf, dateDir, csvOut, testDB, dryRun); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 1
	}

	if !dryRun {
		fmt.Fprintln(os.Stderr, "\nnormalizer: all steps finished OK")
	}
	return 0
}

func runPostExports(asOf time.Time, dateDir, csvOut, testDB string, dryRun bool) error {
	dayDir := paths.DayDir(asOf)
	contractsDir := paths.ContractsDayDir(asOf)

	if csvOut != "" {
		fmt.Fprintf(os.Stderr, "\n>>> csv-export\n")
		if err := export.WriteAggregateCSVs(dayDir, contractsDir, dateDir, csvOut, true); err != nil {
			return fmt.Errorf("csv-export: %w", err)
		}
	}
	if testDB != "" {
		fmt.Fprintf(os.Stderr, "\n>>> test-db\n")
		if err := sqlitedb.PushAll(dayDir, contractsDir, dateDir, testDB, dryRun, true); err != nil {
			return fmt.Errorf("test-db: %w", err)
		}
	}
	return nil
}

func onlyExportSteps(only []string) bool {
	if len(only) == 0 {
		return false
	}
	for _, name := range only {
		switch name {
		case "csv-export", "test-db":
			continue
		default:
			return false
		}
	}
	return true
}

func parseOnly(s string) []string {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}
