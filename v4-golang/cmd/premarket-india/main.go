package main

import (
	"os"

	"github.com/dvygo/premarket/v4g/internal/premarketcmd"
	"github.com/dvygo/premarket/v4g/internal/runner"
)

func main() {
	os.Exit(premarketcmd.Run(premarketcmd.Config{
		LogName:    "premarket-india",
		BinaryName: "premarket-india",
		UsageTitle: "premarket-india — Fyers raw sym_details download (Go)",
		UsageBody: `  premarket-india.exe
  premarket-india.exe --only xnse,xnfo
  premarket-india.exe --only fyers
  premarket-india.exe --include-csv-header
  premarket-india.exe --date-dir 20260629 --dry-run

Normalize / baskets / Postgres: use normalizer.exe.`,
		BuildSteps: runner.BuildIndiaDownloadSteps,
		India:      true,
	}))
}
