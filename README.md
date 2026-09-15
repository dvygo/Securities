# Securities

Symbology and basket pipeline for US and India markets — downloads each venue's
contract master, normalizes every venue into a common schema, assigns a stable
cross-venue instrument token, and optionally pushes to Postgres/ClickHouse.

US venues come from Databento (`GLBX.MDP3`, `EQUS.MINI`, `OPRA.PILLAR`). India
comes from NSE's own contract masters for `XNSE` and from Fyers for `XBOM` and
`XIMC`.

## Layout

| Path | Status | What it is |
|------|--------|------------|
| [`v6-python/`](v6-python/) | **active** | Current pipeline. CLI: `python -m premarketv6 {india,xcme,xcbo,xnas,normalize,check-tokens,check-lineage}`. See [`v6-python/README.md`](v6-python/README.md). |
| [`v5-python/`](v5-python/) | superseded | Previous pipeline. CLI: `python -m premarket {india,xcme,xcbo,xnas,normalize}`. |
| [`v4-golang/`](v4-golang/) | legacy | Earlier Go rewrite. See [`v4-golang/README.md`](v4-golang/README.md) for its own build/run/schema docs. |
| `__________v3_EquityAlgoV20260519-0/` | legacy | Old equity-algo scratch scripts, kept for reference only. |
| `docker/contract-postgres/` | shared | Postgres 16 container the pipelines push into (`docker compose -f docker/contract-postgres/docker-compose.yml up -d`). |

If you're starting fresh, use `v6-python/`. It is where new work lands; the
others are kept for reference and comparison.

## Quick start (v6-python)

```bash
cd v6-python
python -m venv .venv && .venv/bin/pip install -e .[dev]

cp conf/config.ini.example conf/config.ini
# edit conf/config.ini: [databento] api_key, [EXCHANGE:*] venue ids

python -m premarketv6 xcme --all-symbols --today
python -m premarketv6 india
python -m premarketv6 normalize --plugin --csv-only
```

`--csv-only` writes files and never touches a database. The venues publish at
different times of day — a full day is not available before roughly 16:30 IST,
because OPRA lands last — so normalize is designed to be run repeatedly and to
leave already-numbered instruments untouched. Details, including the token
design and the validation commands, are in [`v6-python/README.md`](v6-python/README.md).

Config lives in `conf/config.ini` (gitignored — never commit real API keys).
Basket templates live in `constituents/baskets/`.

## Quick start (v4-golang, legacy)

```powershell
cd v4-golang
copy conf\config.example.ini conf\config.ini
# edit [databento] api_key / api_key_es and [postgres] database_url
.\build.ps1
.\bin\normalizer.exe
```

Full command reference, pipeline stages, and the normalized CSV column spec
are in [`v4-golang/README.md`](v4-golang/README.md).

## Database

All pipelines push into the same local Postgres:

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

Connection string: `postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable`
(the default in every `.ini.example` — only real credentials in your own
local `config.ini` should ever differ from this).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
