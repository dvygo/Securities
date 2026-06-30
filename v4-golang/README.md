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
.\build.ps1
```

Outputs: `bin/premarket.exe`, `bin/normalizer.exe`. Runtime stderr is also written to `bin/LOGS/<binary>_YYYYMMDD_HHMMSS.log`.

## Run

```powershell
.\bin\premarket.exe
.\bin\premarket.exe --date-dir 20260609
.\bin\premarket.exe --include-csv-header
.\bin\premarket.exe --only xnse,xnfo
.\bin\premarket.exe --dry-run

.\bin\normalizer.exe
.\bin\normalizer.exe --date-dir 20260609
.\bin\normalizer.exe --only normalize,baskets
.\bin\normalizer.exe --only postgres
.\bin\normalizer.exe --dry-run
```

## Pipeline

1. **premarket** — Fyers HTTP → `YYYYMMDD/raw/FYERS/*.csv` (headerless, source names e.g. `NSE_CM.csv`)
2. **normalizer** — `YYYYMMDD/normalized/` (14 columns, v2 schema below)
3. **normalizer --only baskets** — `constituents/contracts/YYYYMMDD/*.csv` (`date`, `exchange`, + full normalized v2 row)
4. **normalizer** (default) also pushes Postgres on **contract** DB (`127.0.0.1:7730/cdb`, reads `7731`). Symbology schema `v4_YYYYMMDD`: Fyers `XNSE_FYERS`, `XBOM_FYERS`, `XIMC_FYERS`; NSE exchange `XNSE_NSE_EXCHANGE`, `XNFO_NSE_EXCHANGE`, `XNCD_NSE_EXCHANGE`. Baskets schema `v4_YYYYMMDD_baskets`: one table per contract CSV (`NIFTY_FNO_EQUITY_SPOTS`, `NIFTY500_FUTURES`, …). Compose: `docker/contract-postgres/`. Use `--only normalize,baskets` to skip postgres, or `--only postgres` to push only.

Normalized Fyers outputs merge raw segments per MIC: `XNSE-FYERS.csv` (NSE CM+FO+CD), `XBOM-FYERS.csv` (BSE CM+FO), `XIMC-FYERS.csv` (MCX).

### Normalized CSV columns (v2)

| Column | Source | Notes |
|--------|--------|-------|
| `scriptDetails` | `symDetails` | verbatim |
| `scriptInstrumentType` | `exInstType` | appendix string (`EQ`, `FUTIDX`, `OPTSTK`, …) |
| `scriptInstrumentType2` | `scriptInstrumentType` | `EQ`→`EQUITY`, `FUT*`→`FUTURE`, `OPT*`→`OPTION`, else copy (`INDEX`, `ETF`, …) |
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
| `currency` | — | constant `INR` |

Price scale `100000` matches v3 GLBX/OPRA (`internal/normalize/price.go`). Human price = `strike / 100000` or `tickSize / 100000`.

Fyers appendix codes (exchange, segment, exInstType, fyToken layout, symbology) are hardcoded in `internal/fyers/appendix.go` and `symbology.go`.

### NSE exchange (manual drop)

Place NSE **NEW FILE FORMAT** CSVs under `YYYYMMDD/raw/NSE_EXCHANGE/NEW FILE FORMAT/`:

| File | Staged output (unnormalized copy) |
|------|-----------------------------------|
| `NSE_CM_security.csv` | `XNSE-NSE_EXCHANGE.csv` |
| `NSE_FO_contract.csv` | `XNFO-NSE_EXCHANGE.csv` |
| `NSE_CD_contract.csv` | `XNCD-NSE_EXCHANGE.csv` |

`normalizer --only normalize-nse` (or default `normalize`) copies NSE files byte-for-byte into `normalized/` under the `*-NSE_EXCHANGE.csv` names — no field mapping. Postgres push loads them into `XNSE_NSE_EXCHANGE`, `XNFO_NSE_EXCHANGE`, `XNCD_NSE_EXCHANGE` with the original NSE column names (all TEXT, empty cells as NULL).

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
