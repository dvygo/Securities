package premarketcmd

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

type Config struct {
	LogName    string
	BinaryName string
	UsageTitle string
	UsageBody  string
	BuildSteps func([]string) ([]runner.Step, error)
	India      bool
}

func Run(cfg Config) int {
	var (
		onlyStr          string
		dateDir          string
		dryRun           bool
		inputPath        string
		includeCSVHeader bool
		allSymbols       bool
		symbolsFile      string
		stypeIn          string
		liveStart        int
	)

	flag.StringVar(&onlyStr, "only", "", cfg.onlyHelp())
	flag.StringVar(&dateDir, "date-dir", "", "YYYYMMDD day folder (default: today)")
	flag.BoolVar(&dryRun, "dry-run", false, "print actions only")
	flag.StringVar(&inputPath, "input", "", "local Fyers CSV (headerless or headered; download fallback)")
	flag.BoolVar(&includeCSVHeader, "include-csv-header", false, "write Fyers JSON column names as CSV header on raw files")
	flag.BoolVar(&includeCSVHeader, "include-csv-headers", false, "alias for -include-csv-header")
	flag.BoolVar(&allSymbols, "all-symbols", false, "XCME: subscribe to ALL_SYMBOLS (Live/Hist)")
	flag.StringVar(&symbolsFile, "symbols-file", "", "override symbol list file")
	flag.StringVar(&stypeIn, "stype-in", "", "Databento stype_in override")
	flag.IntVar(&liveStart, "live-start", 0, "Databento Live subscribe start= (default 0)")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "%s\n\n%s\n\nConfig:  %s\nData:    %s/YYYYMMDD/raw/\n\n",
			cfg.UsageTitle, cfg.UsageBody, paths.ConfigINI(), paths.RepoRoot())
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

	cleanup, logPath, err := runlog.Setup(cfg.LogName, dateDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: log setup: %v\n", err)
		return 2
	}
	defer cleanup()
	if !dryRun {
		fmt.Fprintf(os.Stderr, "log:     %s\n", logPath)
	}

	only := parseOnly(onlyStr)
	steps, err := cfg.BuildSteps(only)
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
		AsOf:             asOf,
		DateDir:          dateDir,
		DryRun:           dryRun,
		InputPath:        inputPath,
		IncludeCSVHeader: includeCSVHeader,
		AllSymbols:       allSymbols,
		SymbolsFile:      symbolsFile,
		StypeIn:          stypeIn,
		LiveStart:        liveStart,
	}

	if err := runner.Run(steps, opts); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 1
	}
	if !dryRun {
		fmt.Fprintf(os.Stderr, "\n%s: all steps finished OK\n", cfg.BinaryName)
	}
	return 0
}

func (cfg Config) onlyHelp() string {
	if cfg.India {
		return "segments: comma-separated (fyers,xnse,xnfo,xncd,xbse,xbfo,xmcx); default all"
	}
	return "steps: live, hist, or comma-separated live+hist names; default all for this venue"
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
