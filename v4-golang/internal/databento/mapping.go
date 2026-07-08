package databento

import (
	"bytes"
	"strconv"

	dbn "github.com/NimbleMarkets/dbn-go"
)

// MappingColumns is the v4 raw Databento symbology CSV layout.
var MappingColumns = []string{
	"instrument_id",
	"stype_in_symbol",
	"stype_out_symbol",
	"stype_in",
	"stype_out",
	"start_ts",
	"end_ts",
}

type MappingRow struct {
	InstrumentID   int64
	StypeInSymbol  string
	StypeOutSymbol string
	StypeIn        string
	StypeOut       string
	StartTs        string
	EndTs          string
}

func cleanDBNString(b []byte) string {
	return string(bytes.Trim(b, "\x00"))
}

func formatDBNTimestamp(ts uint64) string {
	if ts == 0 || ts == dbn.UNDEF_TIMESTAMP {
		return ""
	}
	return strconv.FormatUint(ts, 10)
}

func rowFromSymbolMapping(rec *dbn.SymbolMappingMsg) MappingRow {
	row := MappingRow{
		InstrumentID:   int64(rec.Header.InstrumentID),
		StypeInSymbol:  cleanDBNString([]byte(rec.StypeInSymbol)),
		StypeOutSymbol: cleanDBNString([]byte(rec.StypeOutSymbol)),
		StypeIn:        strconv.Itoa(int(rec.StypeIn)),
		StypeOut:       strconv.Itoa(int(rec.StypeOut)),
		StartTs:        formatDBNTimestamp(rec.StartTs),
		EndTs:          formatDBNTimestamp(rec.EndTs),
	}
	return row
}

func rowFromInstrumentDef(rec *dbn.InstrumentDefMsg, stypeInSymbol string, stypeIn dbn.SType) MappingRow {
	return rowFromInstrumentDefFields(rec.Header, rec.Activation, rec.Expiration, cleanDBNString(rec.RawSymbol[:]), stypeInSymbol, stypeIn)
}

func rowFromInstrumentDefFields(hdr dbn.RHeader, activation, expiration uint64, raw, stypeInSymbol string, stypeIn dbn.SType) MappingRow {
	inSym := stypeInSymbol
	if inSym == "" {
		inSym = raw
	}
	return MappingRow{
		InstrumentID:   int64(hdr.InstrumentID),
		StypeInSymbol:  inSym,
		StypeOutSymbol: raw,
		StypeIn:        strconv.Itoa(int(stypeIn)),
		StypeOut:       strconv.Itoa(int(dbn.SType_RawSymbol)),
		StartTs:        formatDBNTimestamp(activation),
		EndTs:          formatDBNTimestamp(expiration),
	}
}

func (r MappingRow) CSVRecord() []string {
	return []string{
		strconv.FormatInt(r.InstrumentID, 10),
		r.StypeInSymbol,
		r.StypeOutSymbol,
		r.StypeIn,
		r.StypeOut,
		r.StartTs,
		r.EndTs,
	}
}
