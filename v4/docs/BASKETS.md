# Baskets and daily contracts (v4)

Curated India instrument lists under `constituents/baskets/` are **membership templates**.  
Daily resolved contracts land in `constituents/contracts/YYYYMMDD/` via `helpers/basket_refresh.py`.

---

## Normalized symbology columns (input)

After `helpers/normalizer.py`, **all** `YYYYMMDD/normalized/*.csv` files (US + India) share the same 9 columns:

| Column | Fyers example | Databento example |
|--------|---------------|-------------------|
| `date` | `20260529` | `20260529` |
| `exchange` | `XNFO` | `XCBO` |
| `underlying_root` | `BANKNIFTY` | `NVDA` |
| `underlying` | `BANKNIFTY` | `NVDA` |
| `strike` | *(empty for FUT)* | `11000000` |
| `expiration` | `20260630` | `20260605` |
| `multiplier` | `30` | `100000` |
| `token` | `scripCode` (per-contract; CM: same as underlying) | Databento `instrument_id` |
| `symbol` | `NSE:BANKNIFTY26JUNFUT` | OCC / Globex / ticker |

**Join key for baskets:** basket lines like `NSE:360ONE-EQ` match normalized `symbol`.

Human display labels for basket output come from raw Fyers `symbol` column (not stored in normalized CSV).

---

## Basket definitions (`constituents/baskets/`)

| File | Role |
|------|------|
| `NIFTY_FNO_EQUITY_SPOTS.csv` | **Source of truth** for Nifty F&O stock universe (`NSE:SYMBOL-EQ`) |
| `NIFTY_FNO_FUTURES_NEAR.csv` | **Derived daily** — front-month FUT per spot underlying |
| `NIFTY_FNO_FUTURES_ALL.csv` | **Derived daily** — all live FUT per spot underlying |
| `NSE_INDEX_FUTURES.csv` | Template — index roots (`NIFTY`, `BANKNIFTY`, …) |
| `BSE_INDEX_FUTURES.csv` | Template — BSE index roots (`SENSEX`, `BANKEX`) |
| `MCX_FUTURES.csv` | Template — commodity roots (`ALUMINI`, `COPPER`, …) |
| `ALL_INDEX_FUTURES.csv` | **Derived daily** — union of NSE + BSE + MCX index/commodity futures |

---

## Contract output (`constituents/contracts/YYYYMMDD/`)

| Column | Source |
|--------|--------|
| `date` | Run date `YYYYMMDD` |
| `exchange` | MIC |
| `underlying` | `underlying` from symbology |
| `instrument` | `SPOT` / `FUT` |
| `expiration` | `YYYYMMDD` or `0` |
| `strike` | Paise or `0` / empty |
| `multiplier` | Lot size |
| `exToken` | `token` |
| `exSymbol` | `symbol` |
| `displaySymbol` | raw Fyers human `symbol` (when available) |

---

## Commands

```powershell
cd v4
python helpers/basket_refresh.py --date-dir 20260529
python runner.py --only normalize baskets --date-dir 20260529
```

Requires normalized symbology for the same `--date-dir` (run `normalize` first).
