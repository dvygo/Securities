# Handoff: Postgres symbology (v4)

For downstream services querying **daily symbology** (US Databento Live + India Fyers).

---

## Connection

| Role | Host | Port | DB | Notes |
|------|------|------|-----|--------|
| **Primary (writes)** | `127.0.0.1` | **7710** | `central` | Loads run here only |
| **Replica (reads)** | `127.0.0.1` | **7711** | `central` | Streaming replication from primary |

Default URL (in `conf/config.ini` / `conf/config.example.ini`):

```
postgres://central:central_pass@127.0.0.1:7710/central?sslmode=disable
```

Override with env `DATABASE_URL` or `[postgres] database_url` in `conf/config.ini`.

**Do not write to 7711.** Schema/table DDL and `COPY` go to **7710**; replica catches up via replication.

---

## Partitioning model

**Schema per trading day** — not hash/list partitions inside a table.

- Each day = one Postgres **schema** named `v2-YYYYMMDD` (quoted identifier), e.g. `"v2-20260529"`.
- On-disk folders remain `v4/YYYYMMDD/`; `--date-dir` selects the CSV folder, push targets `"v2-YYYYMMDD"`.
- Inside each schema: **nine** tables (US + India), same **9-column** layout.
- Legacy `"YYYYMMDD"` schemas (old 14-column layout) are left untouched.
- Reload for a day: **DROP TABLE → CREATE → COPY** (full snapshot replace for that day only).

```
central
└── "v2-20260529"       ← schema = v2 + trading date
    ├── equs_mini
    ├── glbx_mdp3
    ├── opra_pillar
    ├── nse_cm
    ├── nse_fo
    ├── nse_cd
    ├── bse_cm
    ├── bse_fo
    └── mcx_com
└── "20260521"          ← legacy (pre-v2 schema)
    └── ...
```

**Why not hash partitions on `strike` / `underlying`?**  
B-tree indexes on those columns match typical filters (`underlying + expiration`, strike lookups). Hash partitions don’t help range/chain queries and add ops cost for v1.

---

## Tables (per schema)

| Table | Source CSV | Dataset | Loaded? |
|-------|------------|---------|---------|
| `equs_mini` | `normalized/XNAS-DATABENTO.csv` | EQUS.MINI equities | Yes |
| `glbx_mdp3` | `normalized/XCME-DATABENTO.csv` | GLBX.MDP3 futures/options | Yes |
| `opra_pillar` | `normalized/XCBO-DATABENTO.csv` | OPRA.PILLAR options (**stripped** near-term) | Yes |
| `nse_cm` | `normalized/XNSE-FYERS.csv` | NSE cash (Fyers `NSE_CM`) | Yes |
| `nse_fo` | `normalized/XNFO-FYERS.csv` | NSE F&O / NFO (Fyers `NSE_FO`) | Yes |
| `nse_cd` | `normalized/XNCD-FYERS.csv` | NSE currency (Fyers `NSE_CD`) | Yes |
| `bse_cm` | `normalized/XBSE-FYERS.csv` | BSE cash (Fyers `BSE_CM`) | Yes |
| `bse_fo` | `normalized/XBFO-FYERS.csv` | BSE F&O (Fyers `BSE_FO`) | Yes |
| `mcx_com` | `normalized/XMCX-FYERS.csv` | MCX commodity (Fyers `MCX_COM`) | Yes |
| — | `raw/XCBO-DATABENTO.csv` | Full OPRA pull | **No** (v1) |

All loaded tables share the **same 9 columns** (normalized symbology). Raw vendor fields remain in `raw/` only.

---

## Column schema

| Column | Type | Meaning |
|--------|------|---------|
| `date` | `INTEGER NOT NULL` | Trading day `YYYYMMDD` |
| `exchange` | `TEXT` | Venue MIC |
| `underlying_root` | `TEXT` | Root family (e.g. `ES`, `NVDA`, `BANKNIFTY`) |
| `underlying` | `TEXT` | Resolved underlying |
| `strike` | `BIGINT` | Scaled integer; see semantics below |
| `expiration` | `BIGINT` | `YYYYMMDD` expiry, or `0` for spots |
| `multiplier` | `BIGINT` | Contract scale |
| `token` | `BIGINT NOT NULL` | Exchange / Databento instrument token |
| `symbol` | `TEXT` | Trading symbol (OCC, Globex, `NSE:…`) |

### Strike / multiplier semantics (important)

All venues store **`strike` as a scaled integer**: `human_price × multiplier` (OPRA: `int(OCC_8digit) * multiplier // 1000`).

Human price: `strike / multiplier` (use numeric/float for fractional dollars, e.g. $222.50).

**Divisibility:** `strike % multiplier == 0` for whole-dollar strikes; half-dollar and sub-dollar OCC prices (e.g. $222.50) keep exact scaled `strike` but may have a non-zero remainder (e.g. `22250000 % 100000 = 50000`). Prefer `strike / multiplier` in SQL, not `strike % multiplier = 0`, when filtering by price.

| Venue | `strike` | `multiplier` | `expiration` |
|-------|----------|--------------|--------------|
| **OPRA** | `int(OCC_8digit) * multiplier // 1000` (e.g. `00110000` → `11000000`) | `100000` | Option expiry `YYYYMMDD` |
| **GLBX** | Globex price × `multiplier` (e.g. `C7070` → `707000000`) | `100000` | Computed from root + symbol (e.g. `E1A` weekday logic) |
| **EQUS** | `0` (spot) | `1` | `0` |
| **Fyers CM** | `0` (spot) | `1` | `0` |
| **Fyers FO/CD/MCX** | `strikePrice × 100` (paise) | `lotSize` | `expiryDate` → `YYYYMMDD` |

**Fyers source:** `token` = `scripCode` (exchange contract token); `symbol` = `symbolTicker`.

**Dedup on load:** One row per `(token, symbol)` — first CSV row wins.

---

## Indexes (same on all three tables)

| Index | Columns | Use |
|-------|---------|-----|
| `{table}_token_symbol_uq` | **UNIQUE** `(token, symbol)` | Join / dedupe key |
| `{table}_underlying_expiration_idx` | `(underlying, expiration)` | Chains, expiry filters |
| `{table}_exchange_underlying_idx` | `(exchange, underlying)` | Venue + root |
| `{table}_strike_idx` | `(strike)` | Strike filters |
| `{table}_symbol_idx` | `(symbol)` | Direct symbol lookup |

Grants after load: `USAGE` on schema, `SELECT` on all tables in schema (`PUBLIC`).

---

## Example queries

```sql
-- Pin to one trading day (v2 schema)
SET search_path TO "v2-20260529";

-- Row counts
SELECT 'glbx_mdp3' AS t, COUNT(*) FROM glbx_mdp3
UNION ALL SELECT 'opra_pillar', COUNT(*) FROM opra_pillar
UNION ALL SELECT 'equs_mini', COUNT(*) FROM equs_mini;

-- OPRA: scaled strike
SELECT underlying, expiration, strike, multiplier, strike / multiplier AS human_strike, symbol
FROM opra_pillar
WHERE underlying = 'NVDA' AND strike = 11000000
LIMIT 10;

-- GLBX ES chain near expiry
SELECT underlying, expiration, strike, symbol
FROM glbx_mdp3
WHERE underlying = 'ES' AND expiration >= 20260529
ORDER BY expiration, strike
LIMIT 20;

-- List v2 trading-day schemas
SELECT nspname FROM pg_namespace
WHERE nspname ~ '^v2-\d{8}$'
ORDER BY nspname DESC;
```

Fully qualified form:

```sql
SELECT * FROM "v2-20260529".opra_pillar WHERE token = 12345;
```

---

## How data gets there

Pipeline: `runner.py` (from `v4/`)

1. `XCME-DATABENTO.py` → `YYYYMMDD/raw/XCME-DATABENTO.csv`
2. `XCBO-DATABENTO.py` → `YYYYMMDD/raw/XCBO-DATABENTO.csv`
3. `XNAS-DATABENTO.py` → `YYYYMMDD/raw/XNAS-DATABENTO.csv`
4. `XNSE/XNFO/XNCD/XBSE/XBFO/XMCX-FYERS.py` → `YYYYMMDD/raw/*.csv` (Fyers HTTP)
5. `helpers/strip.py` → `YYYYMMDD/normalized/XCBO-DATABENTO.csv`
6. `helpers/normalizer.py` → `YYYYMMDD/normalized/*.csv` (9 columns)
7. `helpers/basket_refresh.py` → `constituents/contracts/YYYYMMDD/`
8. **`helpers/postgres-database-push.py`** (optional) → Postgres schema `"v2-YYYYMMDD"`

```powershell
cd v4
python XNFO-FYERS.py --date-dir 20260529
python helpers/fyers_download.py --all --date-dir 20260529
python helpers/postgres-database-push.py --date-dir 20260521 --dry-run

python runner.py --postgres-push
python runner.py --only fyers normalize
python runner.py --only normalize postgres --date-dir 20260521
```

**Verified sample** (`20260521` on primary): `glbx_mdp3` ~56k, `opra_pillar` ~9.8k, `equs_mini` ~10 rows after dedupe.

---

## Out of scope (v1)

- No Goose/migrations — DDL owned by `postgres-database-push.py`
- No `opra_pillar_unstripped` table
- No writes to replica port
- No in-table hash/list partitioning
- No partial index on `strike > 0` yet (optional later for EQUS)

---

## Related code

| File | Role |
|------|------|
| `helpers/postgres-database-push.py` | DDL, indexes, COPY, dedupe, sentinel handling |
| `runner.py` | `--postgres-push`, `--only postgres` |
| `helpers/normalizer.py` | Column enrichment before load |
| `helpers/symbology_paths.py` | `raw/` + `normalized/` path helpers |
| `helpers/fyers_download.py` | HTTP download Fyers symbol masters |
| `XN*-FYERS.py` | Per-segment download entrypoints |
| `helpers/config.py` | `get_database_url()`, API keys from `conf/config.ini` |
| `conf/config.example.ini` | `[postgres]` template |

Postgres Docker compose lives **outside this repo** (infra handoff).
