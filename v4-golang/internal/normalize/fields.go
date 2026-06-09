package normalize

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/dvygo/premarket/v4g/internal/fyers"
)

func mapFyersRow(row map[string]string) ([]string, error) {
	details := strings.TrimSpace(row["symDetails"])
	if details == "" {
		return nil, fmt.Errorf("missing symDetails")
	}

	instType, ok := fyers.InstrumentTypeNameFromRow(row)
	if !ok {
		code, has := fyers.ExInstTypeFromRow(row)
		if has {
			instType = fmt.Sprintf("UNKNOWN_%d", code)
		} else {
			return nil, fmt.Errorf("missing exInstType")
		}
	}

	tickRaw := strings.TrimSpace(row["tickSize"])
	if tickRaw == "" {
		return nil, fmt.Errorf("missing tickSize")
	}
	tickF, err := strconv.ParseFloat(tickRaw, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid tickSize %q", tickRaw)
	}

	sessionRaw := strings.TrimSpace(row["tradingSession"])
	if sessionRaw == "" {
		return nil, fmt.Errorf("missing tradingSession")
	}
	sessionUTC, ok := TradingSessionISTToUTC(sessionRaw)
	if !ok {
		return nil, fmt.Errorf("invalid tradingSession %q", sessionRaw)
	}

	script := strings.TrimSpace(row["symTicker"])
	if script == "" {
		return nil, fmt.Errorf("missing symTicker")
	}
	token := strings.TrimSpace(row["exToken"])
	if token == "" {
		return nil, fmt.Errorf("missing exToken")
	}
	underlying := strings.TrimSpace(row["exSymName"])
	if underlying == "" {
		return nil, fmt.Errorf("missing exSymName")
	}

	// underlying_root: copy of exSymName for now; reserved for root extraction later.
	underlyingRoot := underlying

	return []string{
		details,
		instType,
		instrumentType2(instType),
		strconv.Itoa(IndiaPriceScale),
		parseLotSize(row["minLotSize"]),
		strconv.FormatInt(ScalePrice(tickF, IndiaPriceScale), 10),
		strings.TrimSpace(row["isin"]),
		sessionUTC,
		expirationNano(row["expiryDate"]),
		script,
		token,
		underlyingRoot,
		underlying,
		strikeScaled(row["strikePrice"]),
		optionTypeResolved(row["optType"]),
	}, nil
}

func parseLotSize(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" {
		return ""
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || v <= 0 {
		return ""
	}
	return strconv.Itoa(int(v))
}

func expirationNano(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" || s == "0" || s == "-1" {
		return ""
	}
	ts, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		f, err2 := strconv.ParseFloat(s, 64)
		if err2 != nil || f <= 0 {
			return ""
		}
		ts = int64(f)
	}
	if ts <= 0 {
		return ""
	}
	var ns int64
	switch {
	case ts < 1_000_000_000_000:
		ns = ts * 1_000_000_000
	case ts < 1_000_000_000_000_000:
		ns = ts * 1_000_000
	default:
		ns = ts
	}
	return strconv.FormatInt(ns, 10)
}

func strikeScaled(raw string) string {
	s := strings.TrimSpace(raw)
	if s == "" || s == "0" || s == "-1" || s == "-1.0" {
		return ""
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || v <= 0 {
		return ""
	}
	return strconv.FormatInt(ScalePrice(v, IndiaPriceScale), 10)
}

func instrumentType2(instType string) string {
	switch strings.ToUpper(strings.TrimSpace(instType)) {
	case "EQ":
		return "SPOT"
	case "FUTSTK", "FUTIDX":
		return "FUTURE"
	default:
		if strings.HasPrefix(strings.ToUpper(strings.TrimSpace(instType)), "OPT") {
			return "OPTION"
		}
		return ""
	}
}

func optionTypeResolved(raw string) string {
	switch strings.ToUpper(strings.TrimSpace(raw)) {
	case fyers.OptTypeCE:
		return "CALL"
	case fyers.OptTypePE:
		return "PUT"
	default:
		return ""
	}
}
