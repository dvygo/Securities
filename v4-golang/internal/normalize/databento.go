package normalize

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/dvygo/premarket/v4g/internal/config"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

type usCfg struct {
	glbxUnderlying string
	glbxMultiplier int
	glbxExchange   string
	opraExchange   string
	opraMultiplier int
	equsExchange   string
}

func loadUSCfg(cfg map[string]string) usCfg {
	def := usCfg{
		glbxUnderlying: "ES",
		glbxMultiplier: IndiaPriceScale,
		glbxExchange:   "XCME",
		opraExchange:   "XCBO",
		opraMultiplier: IndiaPriceScale,
		equsExchange:   "XNAS",
	}
	if v := strings.TrimSpace(cfg["glbx_underlying"]); v != "" {
		def.glbxUnderlying = v
	}
	if v := strings.TrimSpace(cfg["glbx_exchange"]); v != "" {
		def.glbxExchange = v
	}
	if v := strings.TrimSpace(cfg["opra_exchange"]); v != "" {
		def.opraExchange = v
	}
	if v := strings.TrimSpace(cfg["equs_exchange"]); v != "" {
		def.equsExchange = v
	}
	if n, err := strconv.Atoi(strings.TrimSpace(cfg["glbx_multiplier"])); err == nil && n > 0 {
		def.glbxMultiplier = n
	}
	if n, err := strconv.Atoi(strings.TrimSpace(cfg["opra_multiplier"])); err == nil && n > 0 {
		def.opraMultiplier = n
	}
	return def
}

func mapGLBXRow(row map[string]string, asOf time.Time, cfg usCfg) ([]string, bool) {
	stIn := row["stype_in_symbol"]
	stOut := strings.TrimSpace(row["stype_out_symbol"])
	token := strings.TrimSpace(row["instrument_id"])
	if stOut == "" || token == "" {
		return nil, false
	}
	root := underlyingRootFromStypeIn(stIn)
	instType, instType2 := glbxInstType(stOut)

	cols := map[string]string{
		"scriptDetails":        stOut,
		"scriptInstrumentType":   instType,
		"scriptInstrumentType2": instType2,
		"multiplier":           strconv.Itoa(cfg.glbxMultiplier),
		"script":               stOut,
		"scriptToken":          token,
		"underlying_root":      root,
		"underlying":           cfg.glbxUnderlying,
		"currency":             "USD",
		"tradingSessionUTC":    tradingSessionForGLBX(asOf),
	}
	if strike := glbxStrikeInt(stOut, cfg.glbxMultiplier); strike != nil {
		cols["strike"] = strconv.FormatInt(*strike, 10)
	}
	if exp := glbxExpirationYYYYMMDD(root, asOf, stOut); exp != nil {
		cols["expiration"] = yyyymmddToExpirationNs(exp)
	}
	if instType2 == "OPTION" {
		if m := glbxCPStrike.FindStringSubmatch(stOut); len(m) >= 2 {
			switch m[1] {
			case "C":
				cols["optionType"] = "CALL"
			case "P":
				cols["optionType"] = "PUT"
			}
		}
	}
	return mapDatabentoRow(cols), true
}

func mapOPRARow(row map[string]string, asOf time.Time, cfg usCfg) ([]string, bool) {
	stOut := strings.TrimSpace(row["stype_out_symbol"])
	token := strings.TrimSpace(row["instrument_id"])
	if stOut == "" || token == "" {
		return nil, false
	}
	root := underlyingRootFromStypeIn(row["stype_in_symbol"])
	und, exp, strikeTh := parseOPRAOCC(stOut)
	underlying := und
	if underlying == "" {
		underlying = strings.TrimSuffix(root, ".OPT")
		if underlying == "" {
			underlying = root
		}
	}
	instType, instType2 := opraInstType(underlying)

	cols := map[string]string{
		"scriptDetails":        stOut,
		"scriptInstrumentType":   instType,
		"scriptInstrumentType2": instType2,
		"multiplier":           strconv.Itoa(cfg.opraMultiplier),
		"script":               stOut,
		"scriptToken":          token,
		"underlying_root":      strings.TrimSuffix(root, ".OPT"),
		"underlying":           underlying,
		"currency":             "USD",
		"tradingSessionUTC":    tradingSessionForOPRA(underlying, asOf),
	}
	if strikeTh != nil {
		strike := *strikeTh * int64(cfg.opraMultiplier) / 1000
		cols["strike"] = strconv.FormatInt(strike, 10)
	}
	if exp != nil {
		cols["expiration"] = yyyymmddToExpirationNs(exp)
	}
	if m := opraOCCTail.FindStringSubmatch(stOut); len(m) >= 2 {
		switch m[2] {
		case "C":
			cols["optionType"] = "CALL"
		case "P":
			cols["optionType"] = "PUT"
		}
	}
	return mapDatabentoRow(cols), true
}

func mapEQUSRow(row map[string]string, asOf time.Time, cfg usCfg) ([]string, bool) {
	sym := strings.ToUpper(strings.TrimSpace(row["stype_out_symbol"]))
	if sym == "" {
		sym = strings.ToUpper(strings.TrimSpace(row["stype_in_symbol"]))
	}
	token := strings.TrimSpace(row["instrument_id"])
	if sym == "" || token == "" {
		return nil, false
	}
	cols := map[string]string{
		"scriptDetails":         sym,
		"scriptInstrumentType":  "EQ",
		"scriptInstrumentType2": "EQUITY",
		"multiplier":            strconv.Itoa(IndiaPriceScale),
		"script":                sym,
		"scriptToken":           token,
		"underlying_root":       sym,
		"underlying":            sym,
		"currency":              "USD",
		"tradingSessionUTC":     tradingSessionForXNAS(asOf),
	}
	return mapDatabentoRow(cols), true
}

func RunDatabento(asOf time.Time, dryRun bool) error {
	cfgMap := config.LoadNormalizer()
	us := loadUSCfg(cfgMap)
	day := paths.DayDir(asOf)
	fmt.Fprintf(os.Stderr, "normalizer (databento): as_of=%s dir=%s\n", asOf.Format("2006-01-02"), day)

	type job struct {
		name string
		src  string
		fn   func(map[string]string, time.Time, usCfg) ([]string, bool)
	}
	jobs := []job{
		{paths.XCMECSV, paths.DatabentoRawCSV(asOf, paths.XCMECSV), mapGLBXRow},
		{paths.XCBOCSV, paths.DatabentoRawCSV(asOf, paths.XCBOCSV), mapOPRARow},
		{paths.XNASCSV, paths.DatabentoRawCSV(asOf, paths.XNASCSV), mapEQUSRow},
	}

	for _, j := range jobs {
		dst := paths.NormalizedCSV(asOf, j.name)
		if err := normalizeDatabentoFile(j.src, dst, j.fn, asOf, us, dryRun); err != nil {
			return err
		}
	}
	return nil
}
