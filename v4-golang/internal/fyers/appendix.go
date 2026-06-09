package fyers

import (
	"strconv"
	"strings"
)

// Fyers API v3 appendix — https://myapi.fyers.in/docsv3#tag/Appendix
// Hardcoded codes used to resolve raw sym_details during normalization.

// Exchange codes (Appendix: Exchanges).
const (
	ExchangeNSE = 10
	ExchangeMCX = 11
	ExchangeBSE = 12
)

// Segment codes (Appendix: Segments).
const (
	SegmentCM  = 10
	SegmentFO  = 11
	SegmentCD  = 12
	SegmentCOM = 20
)

// Instrument type codes (Appendix: Instrument-Types / exInstType).
const (
	InstEQ          = 0
	InstPrefShares  = 1
	InstDebentures  = 2
	InstWarrants    = 3
	InstMisc        = 4
	InstSGB         = 5
	InstGSecs       = 6
	InstTBills      = 7
	InstMF          = 8
	InstETF         = 9
	InstIndex       = 10
	InstFutIdx      = 11
	InstFutIVX      = 12
	InstFutStk      = 13
	InstOptIdx      = 14
	InstOptStk      = 15
	InstFutCur      = 16
	InstFutIRT      = 17
	InstFutIRC      = 18
	InstOptCur      = 19
	InstUndCur      = 20
	InstUndIRC      = 21
	InstUndIRT      = 22
	InstUndIRD      = 23
	InstIndexCD     = 24
	InstFutIRD      = 25
	InstFutCom      = 30
	InstOptFut      = 31
	InstOptCom      = 32
	InstFutBas      = 33
	InstFutBln      = 34
	InstFutEnr      = 35
	InstOptBln      = 36
	InstOptFutNCOM  = 37
	InstMiscBSE     = 50
)

// Option type codes (Appendix: optType).
const (
	OptTypeNone = "XX"
	OptTypeCE   = "CE"
	OptTypePE   = "PE"
)

// FyToken layout: Exchange (2) + Segment (2) + Expiry YYMMDD (6) + ExToken (2–6 digits).
type FyToken struct {
	Exchange int
	Segment  int
	Expiry   string // YYMMDD; 000000 when not applicable (e.g. CM equity)
	ExToken  string
}

func (t FyToken) HasExpiry() bool {
	return t.Expiry != "" && t.Expiry != "000000"
}

var exchangeName = map[int]string{
	ExchangeNSE: "NSE",
	ExchangeMCX: "MCX",
	ExchangeBSE: "BSE",
}

var segmentName = map[int]string{
	SegmentCM:  "CM",
	SegmentFO:  "FO",
	SegmentCD:  "CD",
	SegmentCOM: "COM",
}

var segmentDescription = map[int]string{
	SegmentCM:  "Capital Market",
	SegmentFO:  "Equity Derivatives",
	SegmentCD:  "Currency Derivatives",
	SegmentCOM: "Commodity Derivatives",
}

var instrumentTypeName = map[int]string{
	InstEQ:         "EQ",
	InstPrefShares: "PREFSHARES",
	InstDebentures: "DEBENTURES",
	InstWarrants:   "WARRANTS",
	InstMisc:       "MISC",
	InstSGB:        "SGB",
	InstGSecs:      "G-SECS",
	InstTBills:     "T-BILLS",
	InstMF:         "MF",
	InstETF:        "ETF",
	InstIndex:      "INDEX",
	InstFutIdx:     "FUTIDX",
	InstFutIVX:     "FUTIVX",
	InstFutStk:     "FUTSTK",
	InstOptIdx:     "OPTIDX",
	InstOptStk:     "OPTSTK",
	InstFutCur:     "FUTCUR",
	InstFutIRT:     "FUTIRT",
	InstFutIRC:     "FUTIRC",
	InstOptCur:     "OPTCUR",
	InstUndCur:     "UNDCUR",
	InstUndIRC:     "UNDIRC",
	InstUndIRT:     "UNDIRT",
	InstUndIRD:     "UNDIRD",
	InstIndexCD:    "INDEX_CD",
	InstFutIRD:     "FUTIRD",
	InstFutCom:     "FUTCOM",
	InstOptFut:     "OPTFUT",
	InstOptCom:     "OPTCOM",
	InstFutBas:     "FUTBAS",
	InstFutBln:     "FUTBLN",
	InstFutEnr:     "FUTENR",
	InstOptBln:     "OPTBLN",
	InstOptFutNCOM: "OPTFUT_NCOM",
	InstMiscBSE:    "MISC_BSE",
}

var instrumentTypeDescription = map[int]string{
	InstEQ:         "Equity Shares",
	InstPrefShares: "Preference Shares",
	InstDebentures: "Collateral-free Debt",
	InstWarrants:   "Warrants on Stock",
	InstMisc:       "Miscellaneous (NSE, BSE)",
	InstSGB:        "Sovereign Gold Bonds",
	InstGSecs:      "Government Securities",
	InstTBills:     "Treasury Bills",
	InstMF:         "Mutual Funds",
	InstETF:        "Exchange Traded Funds",
	InstIndex:      "Stock Market Index",
	InstFutIdx:     "Futures on Index",
	InstFutIVX:     "Futures on Volatility Index",
	InstFutStk:     "Futures on Stock",
	InstOptIdx:     "Options on Index",
	InstOptStk:     "Options on Stock",
	InstFutCur:     "Futures on Currency",
	InstFutIRT:     "Futures on Government of India Treasury Bills",
	InstFutIRC:     "Futures on Government of India Bonds",
	InstOptCur:     "Options on Currency",
	InstUndCur:     "Underlying on Currency",
	InstUndIRC:     "Underlying on Government of Bonds",
	InstUndIRT:     "Underlying on Government of India Treasury Bills",
	InstUndIRD:     "Underlying on 10 Year Notional coupon bearing GOI security",
	InstIndexCD:    "Market-indexed Certificate of deposit",
	InstFutIRD:     "Futures on 10 Year Notional coupon bearing GOI security",
	InstFutCom:     "Futures on Commodity",
	InstOptFut:     "Options on Commodity Futures",
	InstOptCom:     "Options on Commodity",
	InstFutBas:     "Futures on Base Metals",
	InstFutBln:     "Futures on Bullion",
	InstFutEnr:     "Futures on Energy",
	InstOptBln:     "Options on Bullion",
	InstOptFutNCOM: "Options on Commodity Futures (NCOM)",
	InstMiscBSE:    "Miscellaneous (BSE)",
}

// exchangeMIC maps (exchange, segment) -> pipeline MIC.
var exchangeMIC = map[int]map[int]string{
	ExchangeNSE: {
		SegmentCM:  "XNSE",
		SegmentFO:  "XNFO",
		SegmentCD:  "XNCD",
		SegmentCOM: "XNCO",
	},
	ExchangeBSE: {
		SegmentCM:  "XBSE",
		SegmentFO:  "XBFO",
		SegmentCD:  "XBCD",
	},
	ExchangeMCX: {
		SegmentCOM: "XMCX",
	},
}

// ValidExchangeSegment reports whether the appendix lists this exchange+segment pair.
func ValidExchangeSegment(exchange, segment int) bool {
	segMap, ok := exchangeMIC[exchange]
	if !ok {
		return false
	}
	_, ok = segMap[segment]
	return ok
}

// ExchangeName returns NSE/MCX/BSE for a Fyers exchange code.
func ExchangeName(code int) (string, bool) {
	name, ok := exchangeName[code]
	return name, ok
}

// ExchangeNameFromRow reads the exchange field from a raw sym_details row.
func ExchangeNameFromRow(row map[string]string) (string, bool) {
	code, ok := intField(row, "exchange")
	if !ok {
		return "", false
	}
	return ExchangeName(code)
}

// SegmentName returns CM/FO/CD/COM for a Fyers segment code.
func SegmentName(code int) (string, bool) {
	name, ok := segmentName[code]
	return name, ok
}

// SegmentNameFromRow reads the segment field from a raw sym_details row.
func SegmentNameFromRow(row map[string]string) (string, bool) {
	code, ok := intField(row, "segment")
	if !ok {
		return "", false
	}
	return SegmentName(code)
}

// SegmentDescription returns the appendix segment label.
func SegmentDescription(code int) (string, bool) {
	desc, ok := segmentDescription[code]
	return desc, ok
}

// InstrumentTypeName returns the short appendix instrument code (EQ, FUTIDX, ...).
func InstrumentTypeName(code int) (string, bool) {
	name, ok := instrumentTypeName[code]
	return name, ok
}

// InstrumentTypeNameFromRow reads exInstType from a raw sym_details row.
func InstrumentTypeNameFromRow(row map[string]string) (string, bool) {
	code, ok := intField(row, "exInstType")
	if !ok {
		return "", false
	}
	return InstrumentTypeName(code)
}

// InstrumentTypeDescription returns the appendix instrument description.
func InstrumentTypeDescription(code int) (string, bool) {
	desc, ok := instrumentTypeDescription[code]
	return desc, ok
}

// ResolveExchangeMIC maps Fyers (exchange, segment) to pipeline MIC.
func ResolveExchangeMIC(exchange, segment int) (string, bool) {
	segMap, ok := exchangeMIC[exchange]
	if !ok {
		return "", false
	}
	mic, ok := segMap[segment]
	return mic, ok
}

// ResolveExchangeMICFromRow resolves MIC from raw sym_details exchange+segment fields.
func ResolveExchangeMICFromRow(row map[string]string) (string, bool) {
	ex, ok := intField(row, "exchange")
	if !ok {
		return "", false
	}
	seg, ok := intField(row, "segment")
	if !ok {
		return "", false
	}
	return ResolveExchangeMIC(ex, seg)
}

// IsCashInstrument reports whether exInstType is a CM-segment instrument.
func IsCashInstrument(code int) bool {
	switch code {
	case InstEQ, InstPrefShares, InstDebentures, InstWarrants, InstMisc,
		InstSGB, InstGSecs, InstTBills, InstMF, InstETF, InstIndex, InstMiscBSE:
		return true
	default:
		return false
	}
}

// IsFuture reports whether exInstType is a futures contract type.
func IsFuture(code int) bool {
	switch code {
	case InstFutIdx, InstFutIVX, InstFutStk, InstFutCur, InstFutIRT, InstFutIRC,
		InstFutIRD, InstFutCom, InstFutBas, InstFutBln, InstFutEnr:
		return true
	default:
		return false
	}
}

// IsOptionInst reports whether exInstType is an options contract type.
func IsOptionInst(code int) bool {
	switch code {
	case InstOptIdx, InstOptStk, InstOptCur, InstOptFut, InstOptCom, InstOptBln, InstOptFutNCOM:
		return true
	default:
		return false
	}
}

// IsOptionType reports CE/PE (not XX).
func IsOptionType(optType string) bool {
	switch strings.ToUpper(strings.TrimSpace(optType)) {
	case OptTypeCE, OptTypePE:
		return true
	default:
		return false
	}
}

// ParseFyToken splits a fyToken per appendix: 2-digit exchange, 2-digit segment,
// 6-digit expiry (YYMMDD), then 2–6 digit exchange token (CM rows may use 1 digit in practice).
func ParseFyToken(raw string) (FyToken, bool) {
	s := strings.TrimSpace(raw)
	// CM equity tokens can be as short as 11 chars (exToken length 1).
	if len(s) < 11 {
		return FyToken{}, false
	}
	ex, err1 := strconv.Atoi(s[:2])
	seg, err2 := strconv.Atoi(s[2:4])
	if err1 != nil || err2 != nil {
		return FyToken{}, false
	}
	expiry := s[4:10]
	if _, err := strconv.Atoi(expiry); err != nil {
		return FyToken{}, false
	}
	exToken := s[10:]
	if exToken == "" {
		return FyToken{}, false
	}
	for _, c := range exToken {
		if c < '0' || c > '9' {
			return FyToken{}, false
		}
	}
	return FyToken{
		Exchange: ex,
		Segment:  seg,
		Expiry:   expiry,
		ExToken:  exToken,
	}, true
}

// ExInstTypeFromRow reads exInstType from a raw sym_details row.
func ExInstTypeFromRow(row map[string]string) (int, bool) {
	return intField(row, "exInstType")
}

func intField(row map[string]string, key string) (int, bool) {
	raw := strings.TrimSpace(row[key])
	if raw == "" {
		return 0, false
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		f, err2 := strconv.ParseFloat(raw, 64)
		if err2 != nil {
			return 0, false
		}
		return int(f), true
	}
	return v, true
}
