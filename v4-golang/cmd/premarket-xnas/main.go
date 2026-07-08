package main

import (
	"os"

	"github.com/dvygo/premarket/v4g/internal/premarketcmd"
	"github.com/dvygo/premarket/v4g/internal/runner"
)

func main() {
	os.Exit(premarketcmd.Run(premarketcmd.Config{
		LogName:    "premarket-XNAS",
		BinaryName: "premarket-XNAS",
		UsageTitle: "premarket-XNAS — EQUS.MINI Databento symbology (Live + Hist)",
		UsageBody: `  premarket-XNAS.exe
  premarket-XNAS.exe --only live
  premarket-XNAS.exe --only hist
  premarket-XNAS.exe --symbols-file constituents/baskets/XNAS-XCBOE-underlyings.csv
  premarket-XNAS.exe --dry-run`,
		BuildSteps: runner.BuildXNASDownloadSteps,
	}))
}
