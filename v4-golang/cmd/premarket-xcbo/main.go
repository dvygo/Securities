package main

import (
	"os"

	"github.com/dvygo/premarket/v4g/internal/premarketcmd"
	"github.com/dvygo/premarket/v4g/internal/runner"
)

func main() {
	os.Exit(premarketcmd.Run(premarketcmd.Config{
		LogName:    "premarket-XCBO",
		BinaryName: "premarket-XCBO",
		UsageTitle: "premarket-XCBO — OPRA.PILLAR Databento symbology (Live + Hist)",
		UsageBody: `  premarket-XCBO.exe
  premarket-XCBO.exe --only live
  premarket-XCBO.exe --only hist
  premarket-XCBO.exe --symbols-file constituents/baskets/XNAS-XCBOE-underlyings.csv
  premarket-XCBO.exe --dry-run`,
		BuildSteps: runner.BuildXCBODownloadSteps,
	}))
}
