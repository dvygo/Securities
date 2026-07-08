package databento

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"

	dbn "github.com/NimbleMarkets/dbn-go"
	dbn_live "github.com/NimbleMarkets/dbn-go/live"
)

type LiveOpts struct {
	APIKey     string
	Dataset    string
	Symbols    []string
	StypeIn    dbn.SType
	Seconds    float64
	LiveStart  int
	MaxMaps    int
	Retries    int
	RetryDelay float64
}

func LiveSymbolMappings(ctx context.Context, opts LiveOpts) ([]MappingRow, error) {
	var lastErr error
	retries := opts.Retries
	if retries <= 0 {
		retries = 1
	}
	delay := time.Duration(opts.RetryDelay * float64(time.Second))

	var rows []MappingRow
	for attempt := 1; attempt <= retries; attempt++ {
		part, err := liveSymbolMappingsOnce(ctx, opts)
		if err == nil {
			return part, nil
		}
		lastErr = err
		rows = part
		if attempt < retries {
			fmt.Fprintf(os.Stderr, "  Live attempt %d/%d failed: %v; retry in %.0fs\n", attempt, retries, err, opts.RetryDelay)
			select {
			case <-ctx.Done():
				return rows, ctx.Err()
			case <-time.After(delay):
			}
		}
	}
	if len(rows) > 0 {
		return rows, lastErr
	}
	return nil, lastErr
}

func liveSymbolMappingsOnce(ctx context.Context, opts LiveOpts) ([]MappingRow, error) {
	if len(opts.Symbols) == 0 {
		return nil, fmt.Errorf("no symbols to subscribe")
	}

	client, err := dbn_live.NewLiveClient(dbn_live.LiveConfig{
		ApiKey:  opts.APIKey,
		Dataset: opts.Dataset,
	})
	if err != nil {
		return nil, err
	}
	defer client.Stop()

	if _, err := client.Authenticate(opts.APIKey); err != nil {
		return nil, fmt.Errorf("authenticate: %w", err)
	}

	sub := dbn_live.SubscriptionRequestMsg{
		Schema:  "definition",
		StypeIn: opts.StypeIn,
		Symbols: opts.Symbols,
	}
	if opts.LiveStart > 0 {
		sub.Start = time.Unix(0, int64(opts.LiveStart))
	}

	if err := client.Subscribe(sub); err != nil {
		return nil, fmt.Errorf("subscribe: %w", err)
	}
	if err := client.Start(); err != nil {
		return nil, fmt.Errorf("start: %w", err)
	}

	timeout := time.Duration(opts.Seconds * float64(time.Second))
	if timeout <= 0 {
		timeout = 25 * time.Second
	}

	// Match Python: timer closes the session after `seconds`; read synchronously until then.
	stopTimer := time.AfterFunc(timeout, func() { _ = client.Stop() })
	defer stopTimer.Stop()

	scanner := client.GetDbnScanner()
	if scanner == nil {
		return nil, fmt.Errorf("no DBN scanner from live client")
	}

	var rows []MappingRow
	for scanner.Next() {
		if err := ctx.Err(); err != nil {
			return rows, err
		}

		hdr, err := scanner.GetLastHeader()
		if err != nil {
			return rows, err
		}
		if hdr.RType != dbn.RType_SymbolMapping {
			continue
		}
		rec, err := scanner.DecodeSymbolMappingMsg()
		if err != nil {
			return rows, err
		}
		if rec == nil {
			continue
		}
		rows = append(rows, rowFromSymbolMapping(rec))
		if opts.MaxMaps > 0 && len(rows) >= opts.MaxMaps {
			break
		}
	}

	if err := scanner.Error(); err != nil && err != io.EOF && len(rows) == 0 {
		return nil, err
	}
	return rows, nil
}
