# Securities — Premarket 5.0.0

Symbology and basket pipeline for US and India markets. It downloads each venue's
contract master, normalizes every venue into one common schema, rebuilds the
baskets, and pushes the result to the contract Postgres DB.

- **US** comes from Databento: `GLBX.MDP3` (XCME), `EQUS.MINI` (XNAS), `OPRA.PILLAR` (XCBO).
- **India** comes from Fyers: NSE, BSE and MCX segments.

This release ships only `v5-python/`.

## Layout

| Path | What it is |
|------|------------|
| `v5-python/premarket/` | The pipeline. CLI: `python -m premarket {india,xcme,xcbo,xnas,normalize}` |
| `v5-python/conf/` | `config.ini` and `keys.ini`. Copy them from the `.example` files. |
| `v5-python/constituents/baskets/` | Basket templates |
| `v5-python/docker/contract-postgres/` | Postgres 16 container the pipeline pushes into |
| `v5-python/docs/deploy/setup.markdown` | One-time setup: venv, libs, config, contract DB |
| `v5-python/docs/pcg/runbook.markdown` | Daily run: download, normalize, push |
| `data/YYYYMMDD/` | Daily output (`raw/`, `normalized/`, `plugin/`). Not in git. |

## Quick start

Full steps are in [`v5-python/docs/deploy/setup.markdown`](v5-python/docs/deploy/setup.markdown).
Short version:

```bash
cd v5-python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp conf/config.ini.example conf/config.ini
cp conf/keys.ini.example conf/keys.ini     # Databento keys, US venues only

docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

## Daily run

Full steps are in [`v5-python/docs/pcg/runbook.markdown`](v5-python/docs/pcg/runbook.markdown).

```bash
python -m premarket india
python -m premarket xnas          # and/or xcme, xcbo
python -m premarket normalize --postgres-push
```

Add `--date-dir YYYYMMDD` to redo a past day, or `--dry-run` to write nothing.
Logs go to `v5-python/bin/LOGS/`.

## Database

Connection string (the default in `config.ini.example`):

```
postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable
```

Each push writes `v4_YYYYMMDD.contracts`, `v4_YYYYMMDD.baskets`,
`v4_YYYYMMDD_baskets.baskets`, and an always-current copy in `public`.
It drops and recreates the tables every time, so reruns are safe.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
