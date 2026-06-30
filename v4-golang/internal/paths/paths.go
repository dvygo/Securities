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
	NSEExchangeSubdir    = "NSE_EXCHANGE"
	NSENewFormatSubdir   = "NEW FILE FORMAT"
	NormalizedSubdir     = "normalized"
	PostgresSchemaPrefix = "v4_"
)

var NormalizedColumns = []string{
	"scriptDetails",
	"scriptInstrumentType",
	"scriptInstrumentType2",
	"multiplier",
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
	"currency",
}

// ContractColumns is the basket contract CSV layout: run metadata + normalized v2 row.
var ContractColumns = append([]string{"date", "exchange"}, NormalizedColumns...)

const (
	XNSECSV = "XNSE-FYERS.csv"
	XBOMCSV = "XBOM-FYERS.csv"
	XIMCCSV = "XIMC-FYERS.csv"

	XNSENSEEXCHGCSV = "XNSE-NSE_EXCHANGE.csv"
	XNFOEXCHGCSV    = "XNFO-NSE_EXCHANGE.csv"
	XNCDEXCHGCSV    = "XNCD-NSE_EXCHANGE.csv"

	FyersTableSuffix       = "_FYERS"
	NSEExchangeTableSuffix = "_NSE_EXCHANGE"
)

// FyersRawSegment is one headerless Fyers download file (premarket.exe).
type FyersRawSegment struct {
	Key        string
	SourceFile string
}

var FyersRawSegments = []FyersRawSegment{
	{"xnse", "NSE_CM.csv"},
	{"xnfo", "NSE_FO.csv"},
	{"xncd", "NSE_CD.csv"},
	{"xbse", "BSE_CM.csv"},
	{"xbfo", "BSE_FO.csv"},
	{"xmcx", "MCX_COM.csv"},
}

// FyersMICBundle is one normalized output per ISO MIC (normalizer + postgres).
type FyersMICBundle struct {
	ExchangeMIC   string
	OutputCSV     string
	PostgresTable string
	SourceFiles   []string
}

var FyersMICBundles = []FyersMICBundle{
	{"XNSE", XNSECSV, "XNSE" + FyersTableSuffix, []string{"NSE_CM.csv", "NSE_FO.csv", "NSE_CD.csv"}},
	{"XBOM", XBOMCSV, "XBOM" + FyersTableSuffix, []string{"BSE_CM.csv", "BSE_FO.csv"}},
	{"XIMC", XIMCCSV, "XIMC" + FyersTableSuffix, []string{"MCX_COM.csv"}},
}

var (
	fyersRawByKey    map[string]FyersRawSegment
	fyersMICByOutput map[string]FyersMICBundle
)

func init() {
	fyersRawByKey = make(map[string]FyersRawSegment, len(FyersRawSegments))
	for _, s := range FyersRawSegments {
		fyersRawByKey[s.Key] = s
	}
	fyersMICByOutput = make(map[string]FyersMICBundle, len(FyersMICBundles))
	for _, b := range FyersMICBundles {
		fyersMICByOutput[b.OutputCSV] = b
	}
}

func FyersRawSegmentByKey(key string) (FyersRawSegment, error) {
	s, ok := fyersRawByKey[key]
	if !ok {
		return FyersRawSegment{}, fmt.Errorf("unknown Fyers segment %q", key)
	}
	return s, nil
}

func FyersMICForOutputCSV(csvName string) (FyersMICBundle, error) {
	b, ok := fyersMICByOutput[csvName]
	if !ok {
		return FyersMICBundle{}, fmt.Errorf("unknown Fyers output CSV %q", csvName)
	}
	return b, nil
}

type NSESegment struct {
	Key           string
	ExchangeMIC   string
	SourceFile    string
	OutputCSV     string
	PostgresTable string
}

var NSESegments = []NSESegment{
	{"nse_cm", "XNSE", "NSE_CM_security.csv", XNSENSEEXCHGCSV, "XNSE" + NSEExchangeTableSuffix},
	{"nse_fo", "XNFO", "NSE_FO_contract.csv", XNFOEXCHGCSV, "XNFO" + NSEExchangeTableSuffix},
	{"nse_cd", "XNCD", "NSE_CD_contract.csv", XNCDEXCHGCSV, "XNCD" + NSEExchangeTableSuffix},
}

func NSEExchangeRawDir(asOf time.Time) string {
	return filepath.Join(RawDir(asOf), NSEExchangeSubdir, NSENewFormatSubdir)
}

func NSEExchangeRawCSV(asOf time.Time, sourceFile string) string {
	return filepath.Join(NSEExchangeRawDir(asOf), sourceFile)
}

func BinDir() string {
	return filepath.Join(RepoRoot(), "bin")
}

func LogsDir() string {
	return filepath.Join(BinDir(), "LOGS")
}

func EnsureBinDirs() error {
	if err := os.MkdirAll(LogsDir(), 0o755); err != nil {
		return err
	}
	return nil
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

func ConfigINI() string {
	if v := os.Getenv("PREMARKET_CONFIG"); v != "" {
		return v
	}
	return filepath.Join(RepoRoot(), "conf", "config.ini")
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

func PostgresBasketsSchema(dateDir string) (string, error) {
	base, err := PostgresSchema(dateDir)
	if err != nil {
		return "", err
	}
	return base + "_baskets", nil
}
