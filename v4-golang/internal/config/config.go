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
		return nil, fmt.Errorf("secrets not found at %s (set PREMARKET_SECRETS_DIR or create ../secrets/secrets.ini)", p)
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
		"xmcx_exchange": "XMCX",
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
