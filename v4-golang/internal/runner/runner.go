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
	AsOf        time.Time
	DateDir     string
	DryRun      bool
	InputPath   string
	DatabaseURL string
	Basket      string
}

var fyersKeys = []string{"xnse", "xnfo", "xncd", "xbse", "xbfo", "xmcx"}

var allSteps = []Step{
	{Name: "xnse", Run: runFyers("xnse")},
	{Name: "xnfo", Run: runFyers("xnfo")},
	{Name: "xncd", Run: runFyers("xncd")},
	{Name: "xbse", Run: runFyers("xbse")},
	{Name: "xbfo", Run: runFyers("xbfo")},
	{Name: "xmcx", Run: runFyers("xmcx")},
	{Name: "normalize", Run: runNormalize},
	{Name: "baskets", Run: runBaskets},
	{Name: "postgres", Run: runPostgres},
}

func runFyers(key string) func(Opts) error {
	return func(o Opts) error {
		_, err := fyers.DownloadSegment(key, o.AsOf, o.InputPath, o.DryRun)
		return err
	}
}

func runNormalize(o Opts) error {
	cfg := config.LoadNormalizer()
	return normalize.RunFyers(o.AsOf, cfg, o.DryRun)
}

func runBaskets(o Opts) error {
	basket := o.Basket
	if basket == "" {
		basket = "all"
	}
	return baskets.Run(o.AsOf, basket, o.DryRun)
}

func runPostgres(o Opts) error {
	schema, err := paths.PostgresSchema(o.DateDir)
	if err != nil {
		return err
	}
	url, err := config.DatabaseURL(o.DatabaseURL)
	if err != nil {
		return err
	}
	return postgres.PushDay(paths.DayDir(o.AsOf), schema, url, o.DryRun, true)
}

func BuildSteps(only []string, postgresPush bool) ([]Step, error) {
	onlySet := expandOnly(only)
	var steps []Step
	for _, s := range allSteps {
		if s.Name == "postgres" {
			continue
		}
		if onlySet != nil {
			if _, ok := onlySet[s.Name]; !ok {
				continue
			}
		}
		steps = append(steps, s)
	}
	if postgresPush || (onlySet != nil && containsOnly(only, "postgres")) {
		steps = append(steps, Step{Name: "postgres", Run: runPostgres})
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

func expandOnly(only []string) map[string]struct{} {
	if len(only) == 0 {
		return nil
	}
	m := make(map[string]struct{}, len(only))
	for _, name := range only {
		if name == "fyers" {
			for _, k := range fyersKeys {
				m[k] = struct{}{}
			}
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
