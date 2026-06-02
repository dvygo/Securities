# v4-golang — India symbology pipeline (Go)

Go port of `v4/` **without Databento**. Same on-disk layout and Postgres schema (`v2-YYYYMMDD`).

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
```

## Run

```powershell
.\premarket.exe
.\premarket.exe --date-dir 20260602
.\premarket.exe --only fyers,normalize,baskets
.\premarket.exe --postgres-push --date-dir 20260602
.\premarket.exe --dry-run
```

## Pipeline

1. **Fyers HTTP** — six segments → `YYYYMMDD/raw/X*-FYERS.csv`
2. **normalize** — → `YYYYMMDD/normalized/` (9 columns)
3. **baskets** — → `constituents/contracts/YYYYMMDD/*.csv`
4. **postgres** (optional) — India tables: `nse_cm`, `nse_fo`, `nse_cd`, `bse_cm`, `bse_fo`, `mcx_com`

## Environment

| Variable | Purpose |
|----------|---------|
| `PREMARKET_V4G_ROOT` | Override v4-golang data root |
| `PREMARKET_SECRETS_DIR` | Override secrets directory (default: `../secrets`) |
| `DATABASE_URL` | Postgres URL (overrides secrets.ini) |

Basket templates live in `constituents/baskets/` (copied from v4).
