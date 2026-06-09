package postgres

import (
	"bytes"
	"context"
	"encoding/csv"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/dvygo/premarket/v4g/internal/paths"
)

var schemaRE = regexp.MustCompile(`^v2-\d{8}$`)

var colNames = paths.NormalizedColumns

var nullableCols = map[string]struct{}{
	"lotSize": {}, "ISIN": {}, "expiration": {}, "strike": {}, "optionType": {},
}

type tableJob struct {
	table string
	csv   string
}

func indiaJobs() []tableJob {
	var jobs []tableJob
	for _, seg := range paths.FyersSegments {
		jobs = append(jobs, tableJob{seg.PostgresTable, seg.OutputCSV})
	}
	return jobs
}

func PushDay(dayDir, schema, databaseURL string, dryRun, skipMissing bool) error {
	if !schemaRE.MatchString(schema) {
		return fmt.Errorf("invalid schema %q; want v2-YYYYMMDD", schema)
	}
	if st, err := os.Stat(dayDir); err != nil || !st.IsDir() {
		return fmt.Errorf("not found: %s", dayDir)
	}

	var jobs []tableJob
	for _, j := range indiaJobs() {
		p := filepath.Join(dayDir, paths.NormalizedSubdir, j.csv)
		if _, err := os.Stat(p); err != nil {
			if skipMissing {
				fmt.Fprintf(os.Stderr, "skip (missing): %s\n", p)
				continue
			}
			return fmt.Errorf("missing %s", p)
		}
		jobs = append(jobs, j)
	}
	if len(jobs) == 0 {
		return fmt.Errorf("no tables to load")
	}

	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: schema=%s url=%q\n", schema, databaseURL)
		for _, j := range jobs {
			n, err := countCSVRows(filepath.Join(dayDir, paths.NormalizedSubdir, j.csv))
			if err != nil {
				return err
			}
			fmt.Fprintf(os.Stderr, "  %s.%s <- %s (%d rows)\n", schema, j.table, j.csv, n)
		}
		return nil
	}

	ctx := context.Background()
	conn, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)

	tx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, fmt.Sprintf(`CREATE SCHEMA IF NOT EXISTS "%s"`, schema)); err != nil {
		return err
	}

	total := 0
	for _, j := range jobs {
		csvPath := filepath.Join(dayDir, paths.NormalizedSubdir, j.csv)
		n, err := loadTable(ctx, tx, schema, j.table, csvPath)
		if err != nil {
			return err
		}
		if n == 0 {
			fmt.Fprintf(os.Stderr, "skip (empty): %s.%s\n", schema, j.table)
			continue
		}
		total += n
		fmt.Fprintf(os.Stderr, "loaded %d rows -> \"%s\".%s (%s)\n", n, schema, j.table, j.csv)
	}

	grant := fmt.Sprintf(`GRANT USAGE ON SCHEMA "%s" TO PUBLIC; GRANT SELECT ON ALL TABLES IN SCHEMA "%s" TO PUBLIC;`, schema, schema)
	if _, err := tx.Exec(ctx, grant); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "done: schema=%q tables=%d total_rows=%d\n", schema, len(jobs), total)
	return nil
}

func loadTable(ctx context.Context, tx pgx.Tx, schema, table, csvPath string) (int, error) {
	data, nIn, nOut, nSkip, err := csvBytesForCopy(csvPath)
	if err != nil {
		return 0, err
	}
	if len(bytes.TrimSpace(data)) == 0 {
		return 0, nil
	}
	if nSkip > 0 {
		fmt.Fprintf(os.Stderr, "skip: %d malformed rows (%s)\n", nSkip, filepath.Base(csvPath))
	}
	if nOut < nIn {
		fmt.Fprintf(os.Stderr, "dedupe: %d CSV rows -> %d unique (%s)\n", nIn, nOut, filepath.Base(csvPath))
	}

	dropCreate := buildCreateDDL(schema, table)
	if _, err := tx.Exec(ctx, dropCreate); err != nil {
		return 0, err
	}
	for _, idx := range buildIndexDDL(schema, table) {
		if _, err := tx.Exec(ctx, idx); err != nil {
			return 0, err
		}
	}

	copySQL := buildCopySQL(schema, table)
	_, err = tx.Conn().PgConn().CopyFrom(ctx, strings.NewReader(string(data)), copySQL)
	if err != nil {
		return 0, err
	}

	var n int64
	q := fmt.Sprintf(`SELECT COUNT(*)::bigint FROM "%s"."%s"`, schema, table)
	if err := tx.QueryRow(ctx, q).Scan(&n); err != nil {
		return 0, err
	}
	return int(n), nil
}

func buildCreateDDL(schema, table string) string {
	cols := strings.Join([]string{
		`"scriptDetails" TEXT NOT NULL`,
		`"scriptInstrumentType" TEXT NOT NULL`,
		`"lotSize" BIGINT`,
		`"tickSize" BIGINT NOT NULL`,
		`"ISIN" TEXT`,
		`"tradingSessionUTC" TEXT NOT NULL`,
		`"expiration" BIGINT`,
		`"script" TEXT NOT NULL`,
		`"scriptToken" BIGINT NOT NULL`,
		`"underlying_root" TEXT NOT NULL`,
		`"underlying" TEXT NOT NULL`,
		`"strike" BIGINT`,
		`"optionType" TEXT`,
	}, ",\n    ")
	return fmt.Sprintf(`DROP TABLE IF EXISTS "%s"."%s" CASCADE;
CREATE TABLE "%s"."%s" (
    %s
);`, schema, table, schema, table, cols)
}

func buildIndexDDL(schema, table string) []string {
	p := table
	sch, tbl := schema, table
	return []string{
		fmt.Sprintf(`CREATE UNIQUE INDEX IF NOT EXISTS %s_script_token_uq ON "%s"."%s" ("scriptToken", "script")`, p, sch, tbl),
		fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s_underlying_root_expiration_idx ON "%s"."%s" ("underlying_root", "expiration")`, p, sch, tbl),
		fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s_underlying_expiration_idx ON "%s"."%s" ("underlying", "expiration")`, p, sch, tbl),
		fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s_strike_idx ON "%s"."%s" ("strike")`, p, sch, tbl),
		fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s_script_idx ON "%s"."%s" ("script")`, p, sch, tbl),
		fmt.Sprintf(`CREATE INDEX IF NOT EXISTS %s_instrument_type_idx ON "%s"."%s" ("scriptInstrumentType")`, p, sch, tbl),
	}
}

func buildCopySQL(schema, table string) string {
	nulls := sortedKeys(nullableCols)
	quotedCols := make([]string, len(colNames))
	for i, c := range colNames {
		quotedCols[i] = `"` + c + `"`
	}
	return fmt.Sprintf(
		`COPY "%s"."%s" (%s) FROM STDIN WITH (FORMAT csv, HEADER true, ENCODING 'UTF8', FORCE_NULL (%s))`,
		schema, table, strings.Join(quotedCols, ", "), strings.Join(nulls, ", "),
	)
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, `"`+k+`"`)
	}
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

func rowOK(row map[string]string) bool {
	if strings.TrimSpace(row["scriptDetails"]) == "" {
		return false
	}
	if strings.TrimSpace(row["scriptInstrumentType"]) == "" {
		return false
	}
	if strings.TrimSpace(row["tickSize"]) == "" {
		return false
	}
	if strings.TrimSpace(row["tradingSessionUTC"]) == "" {
		return false
	}
	tok := strings.TrimSpace(row["scriptToken"])
	sym := strings.TrimSpace(row["script"])
	if tok == "" || sym == "" {
		return false
	}
	if _, err := parseInt(tok); err != nil {
		return false
	}
	return strings.TrimSpace(row["underlying"]) != "" && strings.TrimSpace(row["underlying_root"]) != ""
}

func parseInt(s string) (int64, error) {
	var v int64
	_, err := fmt.Sscan(s, &v)
	return v, err
}

func csvBytesForCopy(path string) ([]byte, int, int, int, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, 0, 0, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	header, err := r.Read()
	if err != nil {
		return nil, 0, 0, 0, err
	}
	_ = header

	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	if err := w.Write(colNames); err != nil {
		return nil, 0, 0, 0, err
	}
	seen := make(map[string]struct{})
	nIn, nOut, nSkip := 0, 0, 0
	for {
		rec, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, 0, 0, 0, err
		}
		nIn++
		row := make(map[string]string, len(header))
		for i, h := range header {
			if i < len(rec) {
				row[h] = rec[i]
			}
		}
		if !rowOK(row) {
			nSkip++
			continue
		}
		key := strings.TrimSpace(row["scriptToken"]) + "\x00" + strings.TrimSpace(row["script"])
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		out := make([]string, len(colNames))
		for i, c := range colNames {
			out[i] = strings.TrimSpace(row[c])
		}
		if err := w.Write(out); err != nil {
			return nil, 0, 0, 0, err
		}
		nOut++
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return nil, 0, 0, 0, err
	}
	return buf.Bytes(), nIn, nOut, nSkip, nil
}

func countCSVRows(path string) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	r := csv.NewReader(f)
	if _, err := r.Read(); err != nil {
		return 0, err
	}
	n := 0
	for {
		if _, err := r.Read(); err == io.EOF {
			break
		} else if err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}
