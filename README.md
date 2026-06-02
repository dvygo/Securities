# Premarket

Symbology and basket pipeline for US (Databento) and India (Fyers) markets.

## Layout

- `v4/` — current pipeline (`runner.py`, downloads, normalize, baskets, optional Postgres push)
- `__________v3/` — legacy symbology scripts
- `__________v3_EquityAlgoV20260519-0/` — equity algo (output matrices are gitignored)

## Quick start

```powershell
cd v4
python runner.py --exclude databento --date-dir YYYYMMDD
```

Copy `secrets/secrets.example.ini` to `secrets/secrets.ini` and add API keys before running Databento steps.
