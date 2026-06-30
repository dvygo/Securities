package runner

import (
	"fmt"
	"os"
	"time"

	"github.com/dvygo/premarket/v4g/internal/baskets"
	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/fyers"
	"github.com/dvygo/premarket/v4g/internal/normalize"
	"github.com/dvygo/premarket/v4g/internal/paths"
	"github.com/dvygo/premarket/v4g/internal/postgres"
)

type Step struct {
	Name string
	Run  func(Opts) error
}

type Opts struct {
	AsOf             time.Time
	DateDir          string
	DryRun           bool
	InputPath        string
	DatabaseURL      string
	Basket           string
	IncludeCSVHeader bool
}

var fyersKeys = []string{"xnse", "xnfo", "xncd", "xbse", "xbfo", "xmcx"}

var downloadSteps = []Step{
	{Name: "xnse", Run: runFyers("xnse")},
	{Name: "xnfo", Run: runFyers("xnfo")},
	{Name: "xncd", Run: runFyers("xncd")},
	{Name: "xbse", Run: runFyers("xbse")},
	{Name: "xbfo", Run: runFyers("xbfo")},
	{Name: "xmcx", Run: runFyers("xmcx")},
}

var normalizerSteps = []Step{
	{Name: "normalize", Run: runNormalize},
	{Name: "normalize-fyers", Run: runNormalizeFyers},
	{Name: "normalize-nse", Run: runNormalizeNSE},
	{Name: "baskets", Run: runBaskets},
	{Name: "postgres", Run: runPostgres},
}

func runFyers(key string) func(Opts) error {
	return func(o Opts) error {
		_, err := fyers.DownloadSegment(key, fyers.DownloadOpts{
			AsOf:             o.AsOf,
			InputPath:        o.InputPath,
			DryRun:           o.DryRun,
			IncludeCSVHeader: o.IncludeCSVHeader,
		})
		return err
	}
}

func runNormalize(o Opts) error {
	return normalize.RunAll(o.AsOf, o.DryRun)
}

func runNormalizeFyers(o Opts) error {
	return normalize.RunFyers(o.AsOf, o.DryRun)
}

func runNormalizeNSE(o Opts) error {
	return normalize.RunNSE(o.AsOf, o.DryRun)
}

func runBaskets(o Opts) error {
	basket := o.Basket
	if basket == "" {
		basket = "all"
	}
	return baskets.Run(o.AsOf, basket, o.DryRun)
}

func runPostgres(o Opts) error {
	symSchema, err := paths.PostgresSchema(o.DateDir)
	if err != nil {
		return err
	}
	basketsSchema, err := paths.PostgresBasketsSchema(o.DateDir)
	if err != nil {
		return err
	}
	url, err := config.DatabaseURL(o.DatabaseURL)
	if err != nil {
		return err
	}
	return postgres.PushAll(
		paths.DayDir(o.AsOf),
		paths.ContractsDayDir(o.AsOf),
		symSchema,
		basketsSchema,
		url,
		o.DryRun,
		true,
	)
}

// BuildDownloadSteps selects Fyers raw download steps (premarket.exe).
func BuildDownloadSteps(only []string) ([]Step, error) {
	return buildSteps(downloadSteps, only, false, false)
}

// BuildNormalizerSteps selects normalize / baskets / postgres (normalizer.exe).
func BuildNormalizerSteps(only []string, postgresPush bool) ([]Step, error) {
	return buildSteps(normalizerSteps, only, postgresPush, true)
}

func buildSteps(pool []Step, only []string, postgresPush bool, isNormalizer bool) ([]Step, error) {
	onlySet := expandOnly(only, isNormalizer)
	var steps []Step
	for _, s := range pool {
		if onlySet != nil {
			if _, ok := onlySet[s.Name]; !ok {
				continue
			}
		}
		steps = append(steps, s)
	}
	if postgresPush && onlySet != nil {
		if _, ok := onlySet["postgres"]; !ok {
			steps = append(steps, Step{Name: "postgres", Run: runPostgres})
		}
	}
	if len(steps) == 0 {
		return nil, fmt.Errorf("no steps selected")
	}
	return dedupeSteps(steps), nil
}

func containsOnly(only []string, name string) bool {
	for _, s := range only {
		if s == name {
			return true
		}
	}
	return false
}

func expandOnly(only []string, isNormalizer bool) map[string]struct{} {
	if len(only) == 0 {
		return nil
	}
	m := make(map[string]struct{}, len(only))
	for _, name := range only {
		if name == "fyers" && !isNormalizer {
			for _, k := range fyersKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "all" && isNormalizer {
			m["normalize"] = struct{}{}
			m["baskets"] = struct{}{}
			m["postgres"] = struct{}{}
			continue
		}
		if name == "nse" && isNormalizer {
			m["normalize-nse"] = struct{}{}
			continue
		}
		if name == "fyers" && isNormalizer {
			m["normalize-fyers"] = struct{}{}
			continue
		}
		m[name] = struct{}{}
	}
	return m
}

func dedupeSteps(steps []Step) []Step {
	seen := make(map[string]struct{})
	var out []Step
	for _, s := range steps {
		if _, ok := seen[s.Name]; ok {
			continue
		}
		seen[s.Name] = struct{}{}
		out = append(out, s)
	}
	return out
}

func PrereqMissing(name string, asOf time.Time) string {
	switch name {
	case "baskets":
		p := paths.NormalizedCSV(asOf, paths.XNSECSV)
		if _, err := os.Stat(p); err != nil {
			return paths.XNSECSV
		}
	}
	return ""
}

func Run(steps []Step, o Opts) error {
	for _, step := range steps {
		if miss := PrereqMissing(step.Name, o.AsOf); miss != "" {
			fmt.Fprintf(os.Stderr, "skip: %s (missing %s)\n", step.Name, miss)
			continue
		}
		fmt.Fprintf(os.Stderr, "\n>>> %s\n", step.Name)
		if err := step.Run(o); err != nil {
			return fmt.Errorf("%s: %w", step.Name, err)
		}
	}
	return nil
}
