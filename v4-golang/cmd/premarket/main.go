package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/paths"
	"github.com/dvygo/premarket/v4g/internal/runner"
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
		inputPath    string
		databaseURL  string
		basket       string
	)

	flag.StringVar(&onlyStr, "only", "", "steps: comma-separated (xnse,xnfo,...,normalize,baskets,postgres,fyers)")
	flag.StringVar(&dateDir, "date-dir", "", "YYYYMMDD day folder (default: today)")
	flag.BoolVar(&dryRun, "dry-run", false, "print actions only")
	flag.BoolVar(&postgresPush, "postgres-push", false, "load India symbology to Postgres after normalize")
	flag.StringVar(&inputPath, "input", "", "local headerless Fyers CSV (download fallback)")
	flag.StringVar(&databaseURL, "database-url", "", "override postgres URL")
	flag.StringVar(&basket, "basket", "all", "basket name for --only baskets")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, `premarket — v4 India symbology pipeline (Go)

  premarket                          # full Fyers → normalize → baskets
  premarket --date-dir 20260602
  premarket --only fyers normalize
  premarket --only baskets --date-dir 20260602
  premarket --postgres-push
  premarket --dry-run

Secrets: %s
Data:    %s/YYYYMMDD/

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

	only := parseOnly(onlyStr)
	steps, err := runner.BuildSteps(only, postgresPush)
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
		AsOf:        asOf,
		DateDir:     dateDir,
		DryRun:      dryRun,
		InputPath:   inputPath,
		DatabaseURL: databaseURL,
		Basket:      basket,
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
