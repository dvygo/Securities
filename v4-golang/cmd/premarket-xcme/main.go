package main

import (
	"os"

	"github.com/dvygo/premarket/v4g/internal/premarketcmd"
	"github.com/dvygo/premarket/v4g/internal/runner"
)

func main() {
	os.Exit(premarketcmd.Run(premarketcmd.Config{
		LogName:    "premarket-XCME",
		BinaryName: "premarket-XCME",
		UsageTitle: "premarket-XCME — GLBX.MDP3 Databento symbology (Live + Hist)",
		UsageBody: `  premarket-XCME.exe
  premarket-XCME.exe --only live
  premarket-XCME.exe --only hist
  premarket-XCME.exe --only xcme-hist --date-dir 20260629
  premarket-XCME.exe --all-symbols
  premarket-XCME.exe --dry-run`,
		BuildSteps: runner.BuildXCMEDownloadSteps,
	}))
}
