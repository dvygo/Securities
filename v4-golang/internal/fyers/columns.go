package fyers

import "strings"

// JSONColumns are Fyers sym_details field names in column order.
// Used in code only; raw CSV files are headerless by default.
var JSONColumns = []string{
	"fyToken",
	"symDetails",
	"exInstType",
	"minLotSize",
	"tickSize",
	"isin",
	"tradingSession",
	"lastUpdate",
	"expiryDate",
	"symTicker",
	"exchange",
	"segment",
	"exToken",
	"exSymName",
	"underExToken",
	"strikePrice",
	"optType",
	"underFyTok",
	"underSym",
	"fyersExtra1",
	"fyersExtra2",
}

const legacyExtraCols = 4

// LegacyHeaderAliases map pre-v2 CSV header names to JSONColumns keys.
var LegacyHeaderAliases = map[string]string{
	"fytoken":        "fyToken",
	"symbol":         "symDetails",
	"instrumenttype": "exInstType",
	"lotsize":        "minLotSize",
	"isin":           "isin",
	"symbolticker":   "symTicker",
	"scriptcode":     "exToken",
	"scripcode":      "exToken",
	"scripname":      "exSymName",
	"shortsym":       "exSymName",
	"scriptoken":     "underExToken",
	"optiontype":     "optType",
	"underfytoken":   "underFyTok",
	"underexsymbol":  "underSym",
}

func columnCount() int { return len(JSONColumns) }

func legacyColumnCount() int { return len(JSONColumns) - legacyExtraCols }

func normalizeHeaderKey(h string) string {
	key := strings.TrimSpace(h)
	if key == "" {
		return ""
	}
	if canon, ok := legacyHeaderAliases[strings.ToLower(key)]; ok {
		return canon
	}
	for _, col := range JSONColumns {
		if key == col {
			return col
		}
	}
	return key
}

var legacyHeaderAliases = func() map[string]string {
	m := make(map[string]string, len(LegacyHeaderAliases)+len(JSONColumns))
	for k, v := range LegacyHeaderAliases {
		m[k] = v
	}
	for _, col := range JSONColumns {
		m[strings.ToLower(col)] = col
	}
	return m
}()

func isHeaderRow(row []string) bool {
	if len(row) == 0 {
		return false
	}
	first := strings.ToLower(strings.TrimSpace(row[0]))
	if first == "" {
		return false
	}
	_, ok := legacyHeaderAliases[first]
	return ok
}

func padLegacyRow(row []string) []string {
	ncols := columnCount()
	legacy := legacyColumnCount()
	if len(row) == legacy {
		out := make([]string, ncols)
		copy(out, row)
		return out
	}
	return row
}

func rowFromFields(fields []string) map[string]string {
	fields = padLegacyRow(fields)
	m := make(map[string]string, len(JSONColumns))
	for i, col := range JSONColumns {
		if i < len(fields) {
			m[col] = fields[i]
		}
	}
	return m
}

func rowFromHeadered(header, rec []string) map[string]string {
	m := make(map[string]string, len(JSONColumns))
	for i, h := range header {
		key := normalizeHeaderKey(h)
		if key == "" || i >= len(rec) {
			continue
		}
		m[key] = rec[i]
	}
	return m
}
