package runner

import (
	"fmt"
	"os"
	"time"

	"github.com/dvygo/premarket/v4g/internal/baskets"
	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/databento"
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
	AllSymbols       bool
	SymbolsFile      string
	StypeIn          string
	LiveStart        int
}

var databentoLiveKeys = []string{"xcme", "xcbo", "xnas"}
var databentoHistKeys = []string{"xcme-hist", "xcbo-hist", "xnas-hist"}

var normalizerSteps = []Step{
	{Name: "normalize", Run: runNormalize},
	{Name: "normalize-fyers", Run: runNormalizeFyers},
	{Name: "normalize-nse", Run: runNormalizeNSE},
	{Name: "normalize-databento", Run: runNormalizeDatabento},
	{Name: "strip", Run: runStrip},
	{Name: "baskets", Run: runBaskets},
	{Name: "postgres", Run: runPostgres},
}

var fyersKeys = []string{"xnse", "xnfo", "xncd", "xbse", "xbfo", "xmcx"}

func runDatabento(name string, mode databento.Mode) func(Opts) error {
	return func(o Opts) error {
		v, err := databento.ParseVenue(name)
		if err != nil {
			return err
		}
		_, err = databento.Download(databento.DownloadOpts{
			Venue:       v,
			Mode:        mode,
			AsOf:        o.AsOf,
			DryRun:      o.DryRun,
			AllSymbols:  o.AllSymbols,
			SymbolsFile: o.SymbolsFile,
			StypeIn:     o.StypeIn,
			LiveStart:   o.LiveStart,
		})
		return err
	}
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

func runNormalizeDatabento(o Opts) error {
	return normalize.RunDatabento(o.AsOf, o.DryRun)
}

func runStrip(o Opts) error {
	return normalize.RunStrip(o.AsOf, o.DryRun)
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
		if name == "databento" && !isNormalizer {
			for _, k := range databentoLiveKeys {
				m[k] = struct{}{}
			}
			for _, k := range databentoHistKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "databento-live" && !isNormalizer {
			for _, k := range databentoLiveKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "databento-hist" && !isNormalizer {
			for _, k := range databentoHistKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "live" && !isNormalizer {
			for _, k := range databentoLiveKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "hist" && !isNormalizer {
			for _, k := range databentoHistKeys {
				m[k] = struct{}{}
			}
			continue
		}
		if name == "all" && isNormalizer {
			m["normalize"] = struct{}{}
			m["normalize-databento"] = struct{}{}
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
		if name == "databento" && isNormalizer {
			m["normalize-databento"] = struct{}{}
			continue
		}
		if name == "csv-export" && isNormalizer {
			continue // handled via --csv flag in normalizer main
		}
		if name == "test-db" && isNormalizer {
			continue // handled via --test-db flag in normalizer main
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
