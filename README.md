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
| `dockerup.sh` | Builds the image, starts the contract DB, runs the pipeline in Docker |
| `docker/Dockerfile` | The image: Ubuntu 24.04, Python 3.12, pinned libraries, `premarket` and `strategies` compiled by Nuitka |
| `docker/contract-postgres/` | Postgres 16 container the pipeline pushes into |
| `deploy/SETUP.md`, `deploy/RUNBOOK.md` | One-time setup and daily run, with Docker |
| `v5-python/docs/deploy/setup.markdown` | One-time setup without Docker: venv, libs, config, contract DB |
| `v5-python/docs/pcg/runbook.markdown` | Daily run without Docker |
| `data/YYYYMMDD/` | Daily output (`raw/`, `normalized/`, `plugin/`). Not in git. |

## Quick start (Docker)

Full steps are in [`deploy/SETUP.md`](deploy/SETUP.md) and [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).

```bash
./dockerup.sh up          # build securities:latest, create v5-python/conf/*.ini, start the contract DB
./dockerup.sh check       # config, keys, DB connection

./dockerup.sh india       # premarket india
./dockerup.sh normalize   # premarket normalize
./dockerup.sh push        # premarket normalize --only postgres
```

## Quick start (venv)

Full steps are in [`v5-python/docs/deploy/setup.markdown`](v5-python/docs/deploy/setup.markdown).
Short version:

```bash
cd v5-python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp conf/config.ini.example conf/config.ini
cp conf/keys.ini.example conf/keys.ini     # Databento keys, US venues only

docker compose -f ../docker/contract-postgres/docker-compose.yml up -d
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

## Releases

Releases are published to the private release repo
[`deshik-ux/securities`](https://github.com/deshik-ux/securities), which holds only built
artifacts and their deploy files, never source. A release is the `securities:<version>` image
(Ubuntu 24.04 with Python 3.12 and the pinned libraries of `packaging/requirements.lock`) with
`premarket` and `strategies` compiled to native binaries by Nuitka. They are built and published
from the maintainer's machine:

```bash
packaging/release.sh 5.0.0     # build in Docker, run the tests, package into dist/5.0.0/
packaging/publish.sh 5.0.0     # publish dist/5.0.0/ as release v5.0.0 of deshik-ux/securities
```

The version is `v5-python/pyproject.toml`'s. Write the version's section of the release repo's
`CHANGELOG.md` first: `publish.sh` takes the release notes from it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[BSD 3-Clause](LICENSE). Copyright (c) 2026 Deshik Narasimha (dvygo), narasimhadeshik@gmail.com.
