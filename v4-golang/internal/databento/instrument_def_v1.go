package databento

import (
	"encoding/binary"
	"fmt"

	dbn "github.com/NimbleMarkets/dbn-go"
)

// InstrumentDefMsgV1 is the OPRA.PILLAR historical definition layout (22-byte raw_symbol).
const (
	instrumentDefV1BodySize     = 330
	instrumentDefV1Size         = dbn.RHeader_Size + instrumentDefV1BodySize
	instrumentDefV1RawSymbolOff = dbn.RHeader_Size + 184 // same body offset as V2; V1 uses 22-byte cstr
	instrumentDefV1RawSymbolLen = dbn.MetadataV1_SymbolCstrLen
)

type instrumentDefV1 struct {
	Header     dbn.RHeader
	Expiration uint64
	Activation uint64
	RawSymbol  string
}

func decodeInstrumentDefV1(record []byte) (instrumentDefV1, error) {
	if len(record) < instrumentDefV1Size {
		return instrumentDefV1{}, fmt.Errorf("instrument def v1: short record (%d bytes)", len(record))
	}
	var hdr dbn.RHeader
	if err := hdr.Fill_Raw(record[:dbn.RHeader_Size]); err != nil {
		return instrumentDefV1{}, err
	}
	body := record[dbn.RHeader_Size:]
	return instrumentDefV1{
		Header:     hdr,
		Expiration: binary.LittleEndian.Uint64(body[24:32]),
		Activation: binary.LittleEndian.Uint64(body[32:40]),
		RawSymbol: cleanDBNString(record[instrumentDefV1RawSymbolOff : instrumentDefV1RawSymbolOff+instrumentDefV1RawSymbolLen]),
	}, nil
}

func (r instrumentDefV1) mappingRow(stypeInSymbol string, stypeIn dbn.SType) MappingRow {
	return rowFromInstrumentDefFields(r.Header, r.Activation, r.Expiration, r.RawSymbol, stypeInSymbol, stypeIn)
}
