# v4-golang — premarket v2 (Go)

India Fyers + US Databento symbology pipeline. Binaries:

- **premarket-india** — Fyers raw download (six segments)
- **premarket-XCME** — GLBX.MDP3 Live + Hist
- **premarket-XCBO** — OPRA.PILLAR Live + Hist
- **premarket-XNAS** — EQUS.MINI Live + Hist
- **normalizer** — normalize, baskets, Postgres

## Prerequisites

- Go 1.25+
- Config at `conf/config.ini` (copy from `conf/config.example.ini`)

```powershell
copy conf\config.example.ini conf\config.ini
# edit [databento] api_key / api_key_es and [postgres] database_url
```

## Build

```powershell
cd v4-golang
.\build.ps1
```

Outputs: `bin/premarket-india.exe`, `bin/premarket-XCME.exe`, `bin/premarket-XCBO.exe`, `bin/premarket-XNAS.exe`, `bin/normalizer.exe`. Runtime stderr is also written to `bin/LOGS/<binary>_YYYYMMDD_HHMMSS.log`.

## Run

```powershell
.\bin\premarket-india.exe
.\bin\premarket-india.exe --only xnse,xnfo --dry-run

.\bin\premarket-XCME.exe
.\bin\premarket-XCME.exe --only hist --date-dir 20260629
.\bin\premarket-XCME.exe --all-symbols

.\bin\premarket-XCBO.exe
.\bin\premarket-XNAS.exe

.\bin\normalizer.exe
.\bin\normalizer.exe --only normalize-databento
.\bin\normalizer.exe --only normalize,baskets
.\bin\normalizer.exe --only postgres
.\bin\normalizer.exe --date-dir 20260702 --csv test
.\bin\normalizer.exe --date-dir 20260702 --test-db test/test.db
.\bin\normalizer.exe --dry-run
```

## Pipeline

1. **premarket-india** — Fyers HTTP → `YYYYMMDD/raw/FYERS/*.csv`
2. **premarket-XCME / XCBO / XNAS** — Databento → `YYYYMMDD/raw/{XCME,XCBO,XNAS}-DATABENTO.csv`
2. **normalizer** — Fyers/NSE/US Databento → `YYYYMMDD/normalized/` (16-column v2 schema)
3. **normalizer --only strip** (optional) — OPRA near-term filter: raw `XCBO-DATABENTO.csv` → `raw/XCBO-DATABENTO.stripped.csv` (7-col symbology)
4. **normalizer --only baskets** — India contract CSVs under `constituents/contracts/YYYYMMDD/`
5. **normalizer** (default) pushes Postgres schema `v4_YYYYMMDD`: India Fyers/NSE tables + US `glbx_mdp3`, `opra_pillar`, `equs_mini`; baskets schema `v4_YYYYMMDD_baskets`
6. **normalizer --csv DIR** — aggregate `contracts.csv` (all v2 symbology with `date` + `exchange`) and `baskets.csv` (all basket contract rows) under `DIR`
7. **normalizer --test-db PATH** — load two SQLite tables, `contracts` and `baskets` (same aggregated rows as `--csv`; `date` + `exchange` + 16 normalized columns)

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
| `tradingSessionUTC` | `tradingSession` (India) / venue profile (US) | India: IST `HHMM-HHMM` windows → UTC. US: fixed `PRE\|MAIN\|AFTER` slots (`HHMM-HHMM` UTC each), DST via `America/New_York` |
| `expiration` | `expiryDate` | Unix **nanoseconds**; empty for spots |
| `script` | `symTicker` | e.g. `NSE:BANKNIFTY26JUNFUT` |
| `scriptToken` | `exToken` | exchange token |
| `underlying_root` | `exSymName` | copy of `underlying` for now |
| `underlying` | `exSymName` | verbatim |
| `strike` | `strikePrice` | `int(round(price × 100000))` when > 0 |
| `optionType` | `optType` | `CE`→`CALL`, `PE`→`PUT` |
| `currency` | — | `INR` for India Fyers; `USD` for US Databento |

US Databento (`XCME-DATABENTO.csv`, `XCBO-DATABENTO.csv`, `XNAS-DATABENTO.csv`) reads raw 7-column symbology from `YYYYMMDD/raw/`, dedupes by `stype_out_symbol` (latest `start_ts` wins), and maps into the same 16 columns. `script` is the raw Databento symbol (no exchange prefix). `lotSize`, `tickSize`, and `ISIN` are left empty. `tradingSessionUTC` uses three fixed slots `PRE|MAIN|AFTER` (UTC `HHMM-HHMM`; wrap when end crosses midnight). Option strike/expiration/optionType are parsed from OCC/GLBX symbols, not from raw `end_ts`.

| US venue | PRE (ET) | MAIN (ET) | AFTER (ET) |
|----------|----------|-----------|------------|
| XNAS | 04:00–09:30 | 09:30–16:00 | 16:00–20:00 |
| XCBO equity | 07:30–09:25 | 09:30–16:00 | 16:00–16:15 |
| XCBO index (SPX/VIX/RUT) | 20:15–09:25 (wrap) | 09:30–16:15 | 16:15–17:00 |
| XCME Globex | 18:00–09:30 (wrap, excl. 17:00–18:00 maint) | 09:30–16:15 | 16:15–17:00 |

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
| `PREMARKET_CONFIG` | Override config.ini path |
| `DATABENTO_API_KEY` | OPRA + EQUS Databento key |
| `DATABENTO_API_KEY_ES` | GLBX Databento key |
| `DATABASE_URL` | Postgres URL (overrides config.ini) |

Basket templates live in `constituents/baskets/` (copied from v4). Contract CSV columns = `paths.ContractColumns` (`date`, `exchange`, then all 14 normalized fields). Join key: basket `NSE:SYMBOL-EQ` lines match normalized `script`.

### Contract CSV columns

`date`, `exchange`, then the same 14 columns as normalized output (`scriptDetails` … `optionType`). No separate `instrument` / `displaySymbol` — use `scriptInstrumentType` and `scriptDetails` from the normalized row.
