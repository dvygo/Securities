package normalize

import (
	"fmt"
	"strings"
	"sync"
	"time"
)

// etWindow is an Eastern Time session boundary (America/New_York).
type etWindow struct {
	startHour, startMin int
	endHour, endMin     int
}

type sessionProfile struct {
	pre, main, after *etWindow
}

var (
	usEastern     *time.Location
	usEasternOnce sync.Once

	xnasSession = sessionProfile{
		pre:   &etWindow{4, 0, 9, 30},
		main:  &etWindow{9, 30, 16, 0},
		after: &etWindow{16, 0, 20, 0},
	}
	xcboEquitySession = sessionProfile{
		pre:   &etWindow{7, 30, 9, 25},
		main:  &etWindow{9, 30, 16, 0},
		after: &etWindow{16, 0, 16, 15},
	}
	xcboIndexSession = sessionProfile{
		pre:   &etWindow{20, 15, 9, 25}, // overnight GTH (wrap)
		main:  &etWindow{9, 30, 16, 15},
		after: &etWindow{16, 15, 17, 0},
	}
	xcmeGlobexSession = sessionProfile{
		// Excludes daily 17:00–18:00 ET maintenance halt.
		pre:   &etWindow{18, 0, 9, 30}, // Sun 18:00 ET open through RTH start (wrap)
		main:  &etWindow{9, 30, 16, 15},
		after: &etWindow{16, 15, 17, 0},
	}
)

func usEasternLoc() *time.Location {
	usEasternOnce.Do(func() {
		loc, err := time.LoadLocation("America/New_York")
		if err != nil {
			panic("America/New_York: " + err.Error())
		}
		usEastern = loc
	})
	return usEastern
}

func (w etWindow) toUTCSlot(ref time.Time, loc *time.Location) string {
	y, m, d := ref.In(loc).Date()
	start := time.Date(y, m, d, w.startHour, w.startMin, 0, 0, loc)
	end := time.Date(y, m, d, w.endHour, w.endMin, 0, 0, loc)
	if !end.After(start) {
		end = end.AddDate(0, 0, 1)
	}
	return hhmmUTC(start.UTC()) + "-" + hhmmUTC(end.UTC())
}

func hhmmUTC(t time.Time) string {
	return fmt.Sprintf("%02d%02d", t.UTC().Hour(), t.UTC().Minute())
}

func (p sessionProfile) tradingSessionUTC(ref time.Time) string {
	loc := usEasternLoc()
	slots := make([]string, 3)
	wins := []*etWindow{p.pre, p.main, p.after}
	for i, w := range wins {
		if w != nil {
			slots[i] = w.toUTCSlot(ref, loc)
		}
	}
	return strings.Join(slots, "|")
}

func tradingSessionForXNAS(ref time.Time) string {
	return xnasSession.tradingSessionUTC(ref)
}

func tradingSessionForGLBX(ref time.Time) string {
	return xcmeGlobexSession.tradingSessionUTC(ref)
}

func tradingSessionForOPRA(underlying string, ref time.Time) string {
	instType, _ := opraInstType(underlying)
	if instType == "OPTIDX" {
		return xcboIndexSession.tradingSessionUTC(ref)
	}
	return xcboEquitySession.tradingSessionUTC(ref)
}
