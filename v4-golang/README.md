# v4-golang — premarket v2 (Go)

India Fyers symbology pipeline. Two binaries:

- **premarket** — headerless raw Fyers download only
- **normalizer** — normalize, baskets, Postgres (reads `YYYYMMDD/raw/FYERS/`)

## Prerequisites

- Go 1.22+
- Secrets at `../secrets/secrets.ini` (see `secrets/secrets.example.ini`)

```powershell
copy secrets\secrets.example.ini secrets\secrets.ini
# edit database_url if using --postgres-push
```

## Build

```powershell
cd v4-golang
go build -o premarket.exe ./cmd/premarket
go build -o normalizer.exe ./cmd/normalizer
```

## Run

```powershell
.\premarket.exe
.\premarket.exe --date-dir 20260609
.\premarket.exe --include-csv-header
.\premarket.exe --only xnse,xnfo
.\premarket.exe --dry-run

.\normalizer.exe
.\normalizer.exe --date-dir 20260609
.\normalizer.exe --only normalize,baskets
.\normalizer.exe --postgres-push --only normalize,postgres
.\normalizer.exe --dry-run
```

## Pipeline

1. **premarket** — Fyers HTTP → `YYYYMMDD/raw/FYERS/*.csv` (headerless, source names e.g. `NSE_CM.csv`)
2. **normalizer** — `YYYYMMDD/normalized/` (14 columns, v2 schema below)
3. **normalizer --only baskets** — `constituents/contracts/YYYYMMDD/*.csv` (`date`, `exchange`, + full normalized v2 row)
4. **normalizer --postgres-push** — India tables: `nse_cm`, `nse_fo`, `nse_cd`, `bse_cm`, `bse_fo`, `mcx_com`

### Normalized CSV columns (v2)

| Column | Source | Notes |
|--------|--------|-------|
| `scriptDetails` | `symDetails` | verbatim |
| `scriptInstrumentType` | `exInstType` | appendix string (`EQ`, `FUTIDX`, `OPTSTK`, …) |
| `multiplier` | — | constant `100000` (price scale) |
| `lotSize` | `minLotSize` | int; empty if missing |
| `tickSize` | `tickSize` | `int(round(price × 100000))` |
| `ISIN` | `isin` | nullable |
| `tradingSessionUTC` | `tradingSession` | IST `HHMM-HHMM` → UTC (trailing `:` stripped) |
| `expiration` | `expiryDate` | Unix **nanoseconds**; empty for spots |
| `script` | `symTicker` | e.g. `NSE:BANKNIFTY26JUNFUT` |
| `scriptToken` | `exToken` | exchange token |
| `underlying_root` | `exSymName` | copy of `underlying` for now |
| `underlying` | `exSymName` | verbatim |
| `strike` | `strikePrice` | `int(round(price × 100000))` when > 0 |
| `optionType` | `optType` | `CE`→`CALL`, `PE`→`PUT` |

Price scale `100000` matches v3 GLBX/OPRA (`internal/normalize/price.go`). Human price = `strike / 100000` or `tickSize / 100000`.

Fyers appendix codes (exchange, segment, exInstType, fyToken layout, symbology) are hardcoded in `internal/fyers/appendix.go` and `symbology.go`.

Raw Fyers field names (`fyToken`, `symTicker`, `exToken`, …) are used in Go code only. Pass `--include-csv-header` to write them as the first CSV row.

## Environment

| Variable | Purpose |
|----------|---------|
| `PREMARKET_V4G_ROOT` | Override v4-golang data root |
| `PREMARKET_SECRETS_DIR` | Override secrets directory (default: `../secrets`) |
| `DATABASE_URL` | Postgres URL (overrides secrets.ini) |

Basket templates live in `constituents/baskets/` (copied from v4). Contract CSV columns = `paths.ContractColumns` (`date`, `exchange`, then all 14 normalized fields). Join key: basket `NSE:SYMBOL-EQ` lines match normalized `script`.

### Contract CSV columns

`date`, `exchange`, then the same 14 columns as normalized output (`scriptDetails` … `optionType`). No separate `instrument` / `displaySymbol` — use `scriptInstrumentType` and `scriptDetails` from the normalized row.
