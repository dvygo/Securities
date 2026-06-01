# Handoff: Postgres symbology (v3)

For downstream services querying **daily Databento Live symbology** after the v3 pipeline.

---

## Connection

| Role | Host | Port | DB | Notes |
|------|------|------|-----|--------|
| **Primary (writes)** | `127.0.0.1` | **7710** | `central` | Loads run here only |
| **Replica (reads)** | `127.0.0.1` | **7711** | `central` | Streaming replication from primary |

Default URL (in `config.ini` / `config.example.ini`):

```
postgres://central:central_pass@127.0.0.1:7710/central?sslmode=disable
```

Override with env `DATABASE_URL` or `[postgres] database_url` in `config.ini`.

**Do not write to 7711.** Schema/table DDL and `COPY` go to **7710**; replica catches up via replication.

---

## Partitioning model

**Schema per trading day** — not hash/list partitions inside a table.

- Each day = one Postgres **schema** named `YYYYMMDD` (quoted identifier), e.g. `"20260521"`.
- Inside that schema: three tables (same column layout).
- Reload for a day: **DROP TABLE → CREATE → COPY** (full snapshot replace for that day only).
- Other days’ schemas are untouched.

```
central
└── "20260521"          ← schema = trading date
    ├── equs_mini
    ├── glbx_mdp3
    └── opra_pillar
└── "20260522"
    └── ...
```

**Why not hash partitions on `strike` / `underlying`?**  
B-tree indexes on those columns match typical filters (`underlying + expiration`, strike lookups). Hash partitions don’t help range/chain queries and add ops cost for v1.

---

## Tables (per schema)

| Table | Source CSV | Dataset | Loaded? |
|-------|------------|---------|---------|
| `equs_mini` | `equs_mini.csv` | EQUS.MINI equities | Yes |
| `glbx_mdp3` | `glbx_mdp3.csv` | GLBX.MDP3 futures/options | Yes |
| `opra_pillar` | `opra_pillar.csv` | OPRA.PILLAR options (**stripped** near-term) | Yes |
| — | `opra_pillar_unstripped.csv` | Full OPRA pull | **No** (v1) |

All three tables share the **same 14 columns** (normalized symbology).

---

## Column schema

| Column | Type | Meaning |
|--------|------|---------|
| `date` | `INTEGER NOT NULL` | Trading day `YYYYMMDD` (same as schema name) |
| `exchange` | `TEXT` | Venue: `XCME` (GLBX), `XCBO` (OPRA), `XNAS` (EQUS) |
| `underlying_root` | `TEXT` | Root family (e.g. `ES`, `NVDA`) |
| `underlying` | `TEXT` | Resolved underlying (e.g. `ES`, `NVDA`, `SPXW`) |
| `strike` | `BIGINT` | See semantics below |
| `expiration` | `BIGINT` | `YYYYMMDD` expiry, or `0` for spots |
| `multiplier` | `BIGINT` | Contract scale (see below) |
| `instrument_id` | `BIGINT NOT NULL` | Databento instrument id |
| `stype_in_symbol` | `TEXT` | Live subscription symbol |
| `stype_out_symbol` | `TEXT` | Resolved/output symbol (OCC, Globex, ticker) |
| `stype_in` | `BIGINT` | Databento stype enum |
| `stype_out` | `BIGINT` | Databento stype enum |
| `start_ts` | `BIGINT` | Mapping start (ns); `NULL` if open-ended / sentinel |
| `end_ts` | `BIGINT` | Mapping end (ns); `NULL` if open-ended / sentinel |

### Strike / multiplier semantics (important)

All venues store **`strike` as a scaled integer**: `human_price × multiplier` (OPRA: `int(OCC_8digit) * multiplier // 1000`).

Human price: `strike / multiplier` (use numeric/float for fractional dollars, e.g. $222.50).

**Divisibility:** `strike % multiplier == 0` for whole-dollar strikes; half-dollar and sub-dollar OCC prices (e.g. $222.50) keep exact scaled `strike` but may have a non-zero remainder (e.g. `22250000 % 100000 = 50000`). Prefer `strike / multiplier` in SQL, not `strike % multiplier = 0`, when filtering by price.

| Venue | `strike` | `multiplier` | `expiration` |
|-------|----------|--------------|--------------|
| **OPRA** | `int(OCC_8digit) * multiplier // 1000` (e.g. `00110000` → `11000000`) | `100000` | Option expiry `YYYYMMDD` |
| **GLBX** | Globex price × `multiplier` (e.g. `C7070` → `707000000`) | `100000` | Computed from root + symbol (e.g. `E1A` weekday logic) |
| **EQUS** | `0` (spot) | `1` | `0` |

**Timestamps:** Databento sentinel `18446744073709551615` (uint64 max) is stored as **`NULL`** in `start_ts` / `end_ts`.

**Dedup on load:** One row per `(instrument_id, stype_out_symbol)` — first CSV row wins. Raw CSVs can have duplicates; loaded counts are lower (e.g. GLBX ~136k CSV → ~56k rows).

---

## Indexes (same on all three tables)

| Index | Columns | Use |
|-------|---------|-----|
| `{table}_instrument_stype_out_uq` | **UNIQUE** `(instrument_id, stype_out_symbol)` | Join / dedupe key |
| `{table}_underlying_expiration_idx` | `(underlying, expiration)` | Chains, expiry filters |
| `{table}_exchange_underlying_idx` | `(exchange, underlying)` | Venue + root |
| `{table}_strike_idx` | `(strike)` | Strike filters |
| `{table}_stype_out_symbol_idx` | `(stype_out_symbol)` | Direct symbol lookup |

Grants after load: `USAGE` on schema, `SELECT` on all tables in schema (`PUBLIC`).

---

## Example queries

```sql
-- Pin to one trading day
SET search_path TO "20260521";

-- Row counts
SELECT 'glbx_mdp3' AS t, COUNT(*) FROM glbx_mdp3
UNION ALL SELECT 'opra_pillar', COUNT(*) FROM opra_pillar
UNION ALL SELECT 'equs_mini', COUNT(*) FROM equs_mini;

-- OPRA: scaled strike (11000000 = $110 × multiplier 100000)
SELECT underlying, expiration, strike, multiplier, strike / multiplier AS human_strike, stype_out_symbol
FROM opra_pillar
WHERE underlying = 'NVDA' AND strike = 11000000
LIMIT 10;

-- GLBX ES chain near expiry
SELECT underlying, expiration, strike, stype_out_symbol
FROM glbx_mdp3
WHERE underlying = 'ES' AND expiration >= 20260521
ORDER BY expiration, strike
LIMIT 20;

-- Cross-schema: list available days
SELECT nspname FROM pg_namespace
WHERE nspname ~ '^\d{8}$'
ORDER BY nspname DESC;
```

Fully qualified form (no `search_path`):

```sql
SELECT * FROM "20260521".opra_pillar WHERE instrument_id = 12345;
```

---

## How data gets there

Pipeline: `runner.py`

1. `glbx_mdp3.py` → `YYYYMMDD/glbx_mdp3.csv`
2. `opra_pillar.py` → `opra_pillar_unstripped.csv`
3. `equs_mini.py` → `equs_mini.csv`
4. `strip.py` → `opra_pillar.csv`
5. `normalizer.py` → enriches columns on all CSVs
6. **`postgres-database-push.py`** (optional) → Postgres

```powershell
cd __________v3
python postgres-database-push.py --date-dir 20260521
python postgres-database-push.py --date-dir 20260521 --dry-run

python runner.py --postgres-push
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
| `postgres-database-push.py` | DDL, indexes, COPY, dedupe, sentinel handling |
| `runner.py` | `--postgres-push`, `--only postgres` |
| `normalizer.py` | Column enrichment before load |
| `v3_config.py` | `get_database_url()` |
| `config.example.ini` | `[postgres]` template |

Postgres Docker compose lives **outside this repo** (infra handoff).
