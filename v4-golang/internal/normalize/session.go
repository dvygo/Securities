package normalize

import (
	"fmt"
	"strings"
)

const istOffsetMinutes = 5*60 + 30

// TradingSessionISTToUTC converts Fyers tradingSession IST windows to UTC HHMM-HHMM.
func TradingSessionISTToUTC(raw string) (string, bool) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return "", false
	}
	s = strings.TrimSuffix(s, ":")
	parts := strings.Split(s, "|")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		dash := strings.Index(part, "-")
		if dash <= 0 || dash >= len(part)-1 {
			return "", false
		}
		start, err1 := istHHMMToUTC(part[:dash])
		end, err2 := istHHMMToUTC(part[dash+1:])
		if err1 != nil || err2 != nil {
			return "", false
		}
		out = append(out, start+"-"+end)
	}
	if len(out) == 0 {
		return "", false
	}
	return strings.Join(out, "|"), true
}

func istHHMMToUTC(hhmm string) (string, error) {
	hhmm = strings.TrimSpace(hhmm)
	if len(hhmm) != 4 {
		return "", fmt.Errorf("invalid HHMM %q", hhmm)
	}
	for _, c := range hhmm {
		if c < '0' || c > '9' {
			return "", fmt.Errorf("invalid HHMM %q", hhmm)
		}
	}
	h := int(hhmm[0]-'0')*10 + int(hhmm[1]-'0')
	m := int(hhmm[2]-'0')*10 + int(hhmm[3]-'0')
	if h > 23 || m > 59 {
		return "", fmt.Errorf("invalid HHMM %q", hhmm)
	}
	total := h*60 + m - istOffsetMinutes
	for total < 0 {
		total += 24 * 60
	}
	total %= 24 * 60
	return fmt.Sprintf("%02d%02d", total/60, total%60), nil
}
