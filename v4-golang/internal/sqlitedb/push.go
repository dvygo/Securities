package sqlitedb

import (
	"database/sql"
	"fmt"
	"os"
	"strings"

	_ "modernc.org/sqlite"

	"github.com/dvygo/premarket/v4g/internal/export"
	"github.com/dvygo/premarket/v4g/internal/paths"
)

const (
	contractsTable = "contracts"
	basketsTable   = "baskets"
)

// PushAll loads aggregated contracts + baskets tables into a SQLite file.
func PushAll(dayDir, contractsDir, dateStr, dbPath string, dryRun, skipMissing bool) error {
	contractRows, err := export.AggregateContractRows(dayDir, dateStr, skipMissing)
	if err != nil {
		return err
	}
	basketRows, err := export.AggregateBasketRows(contractsDir, skipMissing)
	if err != nil {
		return err
	}
	if len(contractRows) == 0 && len(basketRows) == 0 {
		return fmt.Errorf("no rows to load into sqlite")
	}

	if dryRun {
		fmt.Fprintf(os.Stderr, "dry-run: sqlite=%q table %s (%d rows) table %s (%d rows)\n",
			dbPath, contractsTable, len(contractRows), basketsTable, len(basketRows))
		return nil
	}

	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return err
	}
	defer db.Close()

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	cols := paths.ContractColumns
	if len(contractRows) > 0 {
		n, err := loadTable(tx, contractsTable, cols, contractRows)
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "loaded %d rows -> %s\n", n, contractsTable)
	} else {
		fmt.Fprintf(os.Stderr, "skip (empty): %s\n", contractsTable)
	}
	if len(basketRows) > 0 {
		n, err := loadTable(tx, basketsTable, cols, basketRows)
		if err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "loaded %d rows -> %s\n", n, basketsTable)
	} else {
		fmt.Fprintf(os.Stderr, "skip (empty): %s\n", basketsTable)
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "done: sqlite=%q contracts=%d baskets=%d\n", dbPath, len(contractRows), len(basketRows))
	return nil
}

func loadTable(tx *sql.Tx, table string, cols []string, rows [][]string) (int, error) {
	if err := recreateTable(tx, table, cols); err != nil {
		return 0, err
	}
	return insertRows(tx, table, cols, rows)
}

func recreateTable(tx *sql.Tx, table string, cols []string) error {
	if _, err := tx.Exec(fmt.Sprintf(`DROP TABLE IF EXISTS %q`, table)); err != nil {
		return err
	}
	defs := make([]string, len(cols))
	for i, c := range cols {
		defs[i] = fmt.Sprintf(`%q TEXT`, c)
	}
	ddl := fmt.Sprintf(`CREATE TABLE %q (%s)`, table, strings.Join(defs, ", "))
	_, err := tx.Exec(ddl)
	return err
}

func insertRows(tx *sql.Tx, table string, cols []string, rows [][]string) (int, error) {
	if len(rows) == 0 {
		return 0, nil
	}
	quoted := make([]string, len(cols))
	placeholders := make([]string, len(cols))
	for i, c := range cols {
		quoted[i] = `"` + strings.ReplaceAll(c, `"`, `""`) + `"`
		placeholders[i] = "?"
	}
	stmtSQL := fmt.Sprintf(`INSERT INTO %q (%s) VALUES (%s)`,
		table, strings.Join(quoted, ", "), strings.Join(placeholders, ", "))
	stmt, err := tx.Prepare(stmtSQL)
	if err != nil {
		return 0, err
	}
	defer stmt.Close()

	n := 0
	for _, row := range rows {
		args := make([]any, len(cols))
		for i := range cols {
			val := ""
			if i < len(row) {
				val = row[i]
			}
			if val == "" {
				args[i] = nil
			} else {
				args[i] = val
			}
		}
		if _, err := stmt.Exec(args...); err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}
