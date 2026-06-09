package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
	"github.com/dvygo/premarket/v4g/internal/runlog"
	"github.com/dvygo/premarket/v4g/internal/runner"
)

func main() {
	os.Exit(run())
}

func run() int {
	var (
		onlyStr          string
		dateDir          string
		dryRun           bool
		inputPath        string
		includeCSVHeader bool
	)

	flag.StringVar(&onlyStr, "only", "", "segments: comma-separated (fyers,xnse,xnfo,xncd,xbse,xbfo,xmcx)")
	flag.StringVar(&dateDir, "date-dir", "", "YYYYMMDD day folder (default: today)")
	flag.BoolVar(&dryRun, "dry-run", false, "print actions only")
	flag.StringVar(&inputPath, "input", "", "local Fyers CSV (headerless or headered; download fallback)")
	flag.BoolVar(&includeCSVHeader, "include-csv-header", false, "write Fyers JSON column names as CSV header on raw files")
	flag.BoolVar(&includeCSVHeader, "include-csv-headers", false, "alias for -include-csv-header")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, `premarket v2 — Fyers raw sym_details download (Go)

  premarket                          # download all six Fyers segments (headerless raw)
  premarket --include-csv-header     # raw files with optional CSV header row (alias: --include-csv-headers)
  premarket --date-dir 20260602
  premarket --only fyers             # same as default
  premarket --only xnse,xnfo         # subset of segments
  premarket --dry-run

Normalize / baskets / Postgres: use normalizer.exe (separate binary).

Secrets: %s
Data:    %s/YYYYMMDD/raw/FYERS/

`, paths.ConfigINI(), paths.RepoRoot())
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

	cleanup, logPath, err := runlog.Setup("premarket", dateDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: log setup: %v\n", err)
		return 2
	}
	defer cleanup()
	if !dryRun {
		fmt.Fprintf(os.Stderr, "log:     %s\n", logPath)
	}

	only := parseOnly(onlyStr)
	steps, err := runner.BuildDownloadSteps(only)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}

	if !dryRun {
		fmt.Fprintf(os.Stderr, "root:    %s\n", paths.RepoRoot())
		fmt.Fprintf(os.Stderr, "secrets: %s\n", paths.ConfigINI())
		fmt.Fprintf(os.Stderr, "date-dir: %s\n", dateDir)
	}

	opts := runner.Opts{
		AsOf:             asOf,
		DateDir:          dateDir,
		DryRun:           dryRun,
		InputPath:        inputPath,
		IncludeCSVHeader: includeCSVHeader,
	}

	if err := runner.Run(steps, opts); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 1
	}
	if !dryRun {
		fmt.Fprintln(os.Stderr, "\npremarket: all steps finished OK")
	}
	return 0
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
