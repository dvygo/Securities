package paths

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

const (
	RawSubdir            = "raw"
	FyersSubdir          = "FYERS"
	NormalizedSubdir     = "normalized"
	PostgresSchemaPrefix = "v2-"
)

var NormalizedColumns = []string{
	"scriptDetails",
	"scriptInstrumentType",
	"lotSize",
	"tickSize",
	"ISIN",
	"tradingSessionUTC",
	"expiration",
	"script",
	"scriptToken",
	"underlying_root",
	"underlying",
	"strike",
	"optionType",
}

const (
	XNSECSV = "XNSE-FYERS.csv"
	XNFOCSV = "XNFO-FYERS.csv"
	XNCDCSV = "XNCD-FYERS.csv"
	XBSECSV = "XBSE-FYERS.csv"
	XBFOCSV = "XBFO-FYERS.csv"
	XMCXCSV = "XMCX-FYERS.csv"
)

type FyersSegment struct {
	Key           string
	ExchangeMIC   string
	SourceFile    string
	OutputCSV     string
	PostgresTable string
	CashMarket    bool
}

var FyersSegments = []FyersSegment{
	{"xnse", "XNSE", "NSE_CM.csv", XNSECSV, "nse_cm", true},
	{"xnfo", "XNFO", "NSE_FO.csv", XNFOCSV, "nse_fo", false},
	{"xncd", "XNCD", "NSE_CD.csv", XNCDCSV, "nse_cd", false},
	{"xbse", "XBSE", "BSE_CM.csv", XBSECSV, "bse_cm", true},
	{"xbfo", "XBFO", "BSE_FO.csv", XBFOCSV, "bse_fo", false},
	{"xmcx", "XMCX", "MCX_COM.csv", XMCXCSV, "mcx_com", false},
}

var (
	fyersByKey       map[string]FyersSegment
	fyersByOutputCSV map[string]FyersSegment
)

func init() {
	fyersByKey = make(map[string]FyersSegment, len(FyersSegments))
	fyersByOutputCSV = make(map[string]FyersSegment, len(FyersSegments))
	for _, s := range FyersSegments {
		fyersByKey[s.Key] = s
		fyersByOutputCSV[s.OutputCSV] = s
	}
}

func FyersSegmentByKey(key string) (FyersSegment, error) {
	s, ok := fyersByKey[key]
	if !ok {
		return FyersSegment{}, fmt.Errorf("unknown Fyers segment %q", key)
	}
	return s, nil
}

func FyersSegmentForOutputCSV(csvName string) (FyersSegment, error) {
	s, ok := fyersByOutputCSV[csvName]
	if !ok {
		return FyersSegment{}, fmt.Errorf("unknown Fyers output CSV %q", csvName)
	}
	return s, nil
}

func RepoRoot() string {
	if v := os.Getenv("PREMARKET_V4G_ROOT"); v != "" {
		return v
	}
	// Walk up from cwd looking for go.mod in v4-golang.
	wd, err := os.Getwd()
	if err == nil {
		dir := wd
		for i := 0; i < 8; i++ {
			if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
				if _, err2 := os.Stat(filepath.Join(dir, "internal", "paths")); err2 == nil {
					return dir
				}
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return filepath.Join("..", "v4-golang")
}

func SecretsDir() string {
	if v := os.Getenv("PREMARKET_SECRETS_DIR"); v != "" {
		return v
	}
	return filepath.Join(filepath.Dir(RepoRoot()), "secrets")
}

func SecretsINI() string {
	return filepath.Join(SecretsDir(), "secrets.ini")
}

// ConfigINI is an alias for SecretsINI.
func ConfigINI() string {
	return SecretsINI()
}

func BasketsDir() string {
	return filepath.Join(RepoRoot(), "constituents", "baskets")
}

func ContractsDir() string {
	return filepath.Join(RepoRoot(), "constituents", "contracts")
}

func DayDir(asOf time.Time) string {
	return filepath.Join(RepoRoot(), asOf.Format("20060102"))
}

func RawDir(asOf time.Time) string {
	return filepath.Join(DayDir(asOf), RawSubdir)
}

func FyersRawDir(asOf time.Time) string {
	return filepath.Join(RawDir(asOf), FyersSubdir)
}

func NormalizedDir(asOf time.Time) string {
	return filepath.Join(DayDir(asOf), NormalizedSubdir)
}

// FyersRawCSV is the on-disk path for unnormalized Fyers sym_details (source filename).
func FyersRawCSV(asOf time.Time, sourceFile string) string {
	return filepath.Join(FyersRawDir(asOf), sourceFile)
}

func NormalizedCSV(asOf time.Time, name string) string {
	return filepath.Join(NormalizedDir(asOf), name)
}

func ContractsDayDir(asOf time.Time) string {
	return filepath.Join(ContractsDir(), asOf.Format("20060102"))
}

func PostgresSchema(dateDir string) (string, error) {
	if len(dateDir) != 8 {
		return "", fmt.Errorf("date_dir must be YYYYMMDD, got %q", dateDir)
	}
	for _, c := range dateDir {
		if c < '0' || c > '9' {
			return "", fmt.Errorf("date_dir must be YYYYMMDD, got %q", dateDir)
		}
	}
	return PostgresSchemaPrefix + dateDir, nil
}
