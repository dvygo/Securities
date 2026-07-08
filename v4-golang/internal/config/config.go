package config

import (
	"fmt"
	"os"
	"strings"

	"github.com/go-ini/ini"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

const fyersBaseURL = "https://public.fyers.in/sym_details"

const defaultUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

type Fyers struct {
	BaseURL       string
	UserAgent     string
	TimeoutSec    float64
	Retries       int
	RetryDelaySec float64
}

type Normalizer map[string]string

func loadINI() (*ini.File, error) {
	p := paths.ConfigINI()
	if _, err := os.Stat(p); err != nil {
		return nil, fmt.Errorf("config not found at %s (copy conf/config.example.ini to conf/config.ini or set PREMARKET_CONFIG)", p)
	}
	return ini.Load(p)
}

func LoadFyers() (Fyers, error) {
	def := Fyers{
		BaseURL:       fyersBaseURL,
		UserAgent:     defaultUA,
		TimeoutSec:    120,
		Retries:       3,
		RetryDelaySec: 2,
	}
	f, err := loadINI()
	if err != nil {
		return def, nil // Fyers public URLs work without secrets.
	}
	sec, err := f.GetSection("fyers")
	if err != nil {
		return def, nil
	}
	if v := strings.TrimSpace(sec.Key("base_url").String()); v != "" {
		def.BaseURL = v
	}
	if v := strings.TrimSpace(sec.Key("user_agent").String()); v != "" {
		def.UserAgent = v
	}
	if v, err := sec.Key("timeout_sec").Float64(); err == nil && v > 0 {
		def.TimeoutSec = v
	}
	if v, err := sec.Key("retries").Int(); err == nil && v > 0 {
		def.Retries = v
	}
	if v, err := sec.Key("retry_delay_sec").Float64(); err == nil && v >= 0 {
		def.RetryDelaySec = v
	}
	return def, nil
}

func LoadNormalizer() Normalizer {
	def := Normalizer{
		"xnse_exchange": "XNSE",
		"xnfo_exchange": "XNFO",
		"xncd_exchange": "XNCD",
		"xbse_exchange": "XBSE",
		"xbfo_exchange": "XBFO",
		"xmcx_exchange": "XIMC",
		"glbx_underlying": "ES",
		"glbx_multiplier": "100000",
		"glbx_exchange": "XCME",
		"opra_exchange": "XCBO",
		"opra_multiplier": "100000",
		"equs_exchange": "XNAS",
		"equs_multiplier": "1",
	}
	f, err := loadINI()
	if err != nil {
		return def
	}
	sec, err := f.GetSection("normalizer")
	if err != nil {
		return def
	}
	for k := range def {
		if v := strings.TrimSpace(sec.Key(k).String()); v != "" {
			def[k] = v
		}
	}
	return def
}

func DatabaseURL(override string) (string, error) {
	if v := strings.TrimSpace(override); v != "" {
		return v, nil
	}
	if v := strings.TrimSpace(os.Getenv("DATABASE_URL")); v != "" {
		return v, nil
	}
	f, err := loadINI()
	if err != nil {
		return "", fmt.Errorf("postgres: %w", err)
	}
	sec, err := f.GetSection("postgres")
	if err != nil {
		return "", fmt.Errorf("postgres: missing [postgres] in %s", paths.ConfigINI())
	}
	v := strings.TrimSpace(sec.Key("database_url").String())
	if v == "" {
		return "", fmt.Errorf("postgres: database_url empty in %s", paths.ConfigINI())
	}
	return v, nil
}

type Databento struct {
	APIKey            string
	APIKeyES          string
	LiveSeconds       float64
	LiveRetries       int
	LiveRetryDelaySec float64
	MaxMaps           int
	HistLookbackDays  int
}

func LoadDatabento() (Databento, error) {
	def := Databento{
		LiveSeconds:       25,
		LiveRetries:       3,
		LiveRetryDelaySec: 2,
		MaxMaps:           100_000,
		HistLookbackDays:  7,
	}
	if v := strings.TrimSpace(os.Getenv("DATABENTO_API_KEY")); v != "" {
		def.APIKey = v
	}
	if v := strings.TrimSpace(os.Getenv("DATABENTO_API_KEY_ES")); v != "" {
		def.APIKeyES = v
	}
	f, err := loadINI()
	if err != nil {
		if def.APIKey == "" && def.APIKeyES == "" {
			return def, fmt.Errorf("databento: %w", err)
		}
		return def, nil
	}
	sec, err := f.GetSection("databento")
	if err == nil {
		if def.APIKey == "" {
			def.APIKey = strings.TrimSpace(sec.Key("api_key").String())
		}
		if def.APIKeyES == "" {
			def.APIKeyES = strings.TrimSpace(sec.Key("api_key_es").String())
		}
		if v, e := sec.Key("live_seconds").Float64(); e == nil && v > 0 {
			def.LiveSeconds = v
		}
		if v, e := sec.Key("live_retries").Int(); e == nil && v > 0 {
			def.LiveRetries = v
		}
		if v, e := sec.Key("live_retry_delay_sec").Float64(); e == nil && v >= 0 {
			def.LiveRetryDelaySec = v
		}
		if v, e := sec.Key("max_maps").Int(); e == nil && v > 0 {
			def.MaxMaps = v
		}
		if v, e := sec.Key("hist_lookback_days").Int(); e == nil && v > 0 {
			def.HistLookbackDays = v
		}
	}
	if def.APIKeyES == "" {
		def.APIKeyES = def.APIKey
	}
	if def.APIKey == "" && def.APIKeyES == "" {
		return def, fmt.Errorf("databento: missing api_key in %s (or DATABENTO_API_KEY env)", paths.ConfigINI())
	}
	return def, nil
}

func (d Databento) APIKeyForES(useES bool) (string, error) {
	if useES {
		if d.APIKeyES == "" {
			return "", fmt.Errorf("databento: missing api_key_es for GLBX")
		}
		return d.APIKeyES, nil
	}
	if d.APIKey == "" {
		return "", fmt.Errorf("databento: missing api_key")
	}
	return d.APIKey, nil
}
