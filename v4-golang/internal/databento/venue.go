package databento

import (
	"fmt"
	"strings"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

type Mode int

const (
	ModeLive Mode = iota
	ModeHist
)

func (m Mode) String() string {
	if m == ModeHist {
		return "historical"
	}
	return "live"
}

type Venue int

const (
	VenueXCME Venue = iota
	VenueXCBO
	VenueXNAS
)

func ParseVenue(name string) (Venue, error) {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "xcme":
		return VenueXCME, nil
	case "xcbo":
		return VenueXCBO, nil
	case "xnas":
		return VenueXNAS, nil
	default:
		return VenueXCME, fmt.Errorf("unknown databento venue %q", name)
	}
}

func (v Venue) String() string {
	switch v {
	case VenueXCME:
		return "xcme"
	case VenueXCBO:
		return "xcbo"
	case VenueXNAS:
		return "xnas"
	default:
		return "unknown"
	}
}

func (v Venue) Dataset() string {
	switch v {
	case VenueXCME:
		return "GLBX.MDP3"
	case VenueXCBO:
		return "OPRA.PILLAR"
	case VenueXNAS:
		return "EQUS.MINI"
	default:
		return ""
	}
}

func (v Venue) OutputCSV() string {
	switch v {
	case VenueXCME:
		return paths.XCMECSV
	case VenueXCBO:
		return paths.XCBOCSV
	case VenueXNAS:
		return paths.XNASCSV
	default:
		return ""
	}
}

func (v Venue) UsesESKey() bool {
	return v == VenueXCME
}

func (v Venue) DefaultStypeIn(allSymbols bool) string {
	switch v {
	case VenueXCME:
		if allSymbols {
			return "raw_symbol"
		}
		return "parent"
	case VenueXCBO:
		return "parent"
	case VenueXNAS:
		return "raw_symbol"
	default:
		return "raw_symbol"
	}
}

func (v Venue) PerSymbolSessions(allSymbols, symbolsFile bool) bool {
	if allSymbols || symbolsFile {
		return false
	}
	switch v {
	case VenueXCME:
		return true // built-in ES parent list
	case VenueXCBO, VenueXNAS:
		return true // one session per underlying
	default:
		return false
	}
}
