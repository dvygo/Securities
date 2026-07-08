package runner

import (
	"github.com/dvygo/premarket/v4g/internal/databento"
)

var indiaDownloadSteps = []Step{
	{Name: "xnse", Run: runFyers("xnse")},
	{Name: "xnfo", Run: runFyers("xnfo")},
	{Name: "xncd", Run: runFyers("xncd")},
	{Name: "xbse", Run: runFyers("xbse")},
	{Name: "xbfo", Run: runFyers("xbfo")},
	{Name: "xmcx", Run: runFyers("xmcx")},
}

var xcmeDownloadSteps = []Step{
	{Name: "xcme", Run: runDatabento("xcme", databento.ModeLive)},
	{Name: "xcme-hist", Run: runDatabento("xcme", databento.ModeHist)},
}

var xcboDownloadSteps = []Step{
	{Name: "xcbo", Run: runDatabento("xcbo", databento.ModeLive)},
	{Name: "xcbo-hist", Run: runDatabento("xcbo", databento.ModeHist)},
}

var xnasDownloadSteps = []Step{
	{Name: "xnas", Run: runDatabento("xnas", databento.ModeLive)},
	{Name: "xnas-hist", Run: runDatabento("xnas", databento.ModeHist)},
}

// BuildIndiaDownloadSteps selects Fyers raw download steps (premarket-india.exe).
func BuildIndiaDownloadSteps(only []string) ([]Step, error) {
	return buildSteps(indiaDownloadSteps, only, false, false)
}

// BuildXCMEDownloadSteps selects GLBX Live + Hist (premarket-XCME.exe).
func BuildXCMEDownloadSteps(only []string) ([]Step, error) {
	return buildSteps(xcmeDownloadSteps, only, false, false)
}

// BuildXCBODownloadSteps selects OPRA Live + Hist (premarket-XCBO.exe).
func BuildXCBODownloadSteps(only []string) ([]Step, error) {
	return buildSteps(xcboDownloadSteps, only, false, false)
}

// BuildXNASDownloadSteps selects EQUS Live + Hist (premarket-XNAS.exe).
func BuildXNASDownloadSteps(only []string) ([]Step, error) {
	return buildSteps(xnasDownloadSteps, only, false, false)
}

// BuildDownloadSteps is all download steps (legacy combined pool).
func BuildDownloadSteps(only []string) ([]Step, error) {
	pool := append(append(append(append([]Step{}, xcmeDownloadSteps...), xcboDownloadSteps...), xnasDownloadSteps...), indiaDownloadSteps...)
	return buildSteps(pool, only, false, false)
}
