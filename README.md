# Securities

Symbology and basket pipeline for US (Databento) and India (Fyers) markets —
downloads raw exchange/vendor data, normalizes it into a common 16-column
schema, builds index/futures baskets, and optionally pushes everything to
Postgres.

## Layout

| Path | Status | What it is |
|------|--------|------------|
| [`v5-python/`](v5-python/) | **active** | Current pipeline (Python). CLI: `python -m premarket {india,xcme,xcbo,xnas,normalize}`. |
| [`v4-golang/`](v4-golang/) | maintained | Earlier Go rewrite of the same pipeline. See [`v4-golang/README.md`](v4-golang/README.md) for its own build/run/schema docs. |
| `__________v3_EquityAlgoV20260519-0/` | legacy | Old equity-algo scratch scripts, kept for reference only. |
| `docker/contract-postgres/` | shared | Postgres 16 container both pipelines push into (`docker compose -f docker/contract-postgres/docker-compose.yml up -d`). |

If you're starting fresh, use `v5-python/`. `v4-golang/` is still maintained
in parallel but isn't where new work lands.

## Quick start (v5-python)

```bash
cd v5-python
python -m venv .venv && .venv/Scripts/pip install -e .[dev]   # or source .venv/bin/activate on Linux/macOS

cp conf/config.ini.example conf/config.ini
# edit conf/config.ini: [databento] api_key/api_key_es, [postgres] database_url

python -m premarket india
python -m premarket xcme --date-dir 20260710
python -m premarket normalize
```

Config lives in `conf/config.ini` (gitignored — never commit real API keys).
Basket templates live in `constituents/baskets/`.

## Quick start (v4-golang)

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

Both pipelines push into the same local Postgres:

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

Connection string: `postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable`
(the default in both `.ini.example` files — only real credentials in your
own local `config.ini` should ever differ from this).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
