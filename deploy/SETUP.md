# Setup

One time per host. Builds the v5 image from this checkout and starts the contract DB. Daily commands are in [RUNBOOK.md](RUNBOOK.md).

```mermaid
flowchart TB
    A["Install Docker"] --> B["git clone the repo, checkout the release branch"]
    B --> C["./dockerup.sh up"]
    C --> D["Edit v5-python/conf/config.ini"]
    D --> E["Edit v5-python/conf/keys.ini"]
    E --> F["./dockerup.sh check"]
```

## 1. Install Docker

```sh
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # then log out and back in
docker compose version
```

## 2. Get the code

```sh
git clone git@github.com:dvygo/Securities.git
cd Securities
git checkout release-pipeline
```

## 3. Build and start

```sh
./dockerup.sh up
```

This:

1. builds `securities:latest` from `docker/Dockerfile` (unit tests, then Nuitka compile; a few minutes)
2. creates `v5-python/conf/config.ini` and `keys.ini` from the `.example` files
3. starts the contract DB: Postgres 16, port `6006`, login `contract`/`contract`

## 4. Contract DB

```mermaid
flowchart LR
    Q{"Contract DB on this host?"} -->|yes| L["Nothing to do: up started it"]
    Q -->|no| X["export CONTRACT_DB=external, set database_url below"]
```

`v5-python/conf/config.ini`:

```ini
[postgres]
database_url = postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable
```

For another server: `postgres://USER:PASSWORD@HOST:6006/contractdb?sslmode=disable`. URL-encode `@` in a password as `%40`.

Exposed to the network? Change `POSTGRES_USER`/`POSTGRES_PASSWORD` in `docker/contract-postgres/docker-compose.yml` **before** the first `up`.

## 5. Databento keys

```mermaid
flowchart LR
    Q{"Running xnas, xcme, xcbo or strategies?"} -->|yes| K["Fill v5-python/conf/keys.ini"]
    Q -->|no| N["Leave it empty: India needs no key"]
```

`v5-python/conf/keys.ini`:

```ini
[production]
key_XNAS=db-...
key_XCBO=db-...
key_XCME=db-...
```

## 6. Check

```sh
./dockerup.sh check
```

```text
config:    v5-python/conf/config.ini
config:    v5-python/conf/keys.ini
databento: [production] keys for: XNAS XCBO XCME
database:  ok (127.0.0.1:6006/contractdb?sslmode=disable)
```

```mermaid
flowchart LR
    Q{"database: ok?"} -->|yes| R["Done. Go to RUNBOOK.md"]
    Q -->|no| S{"./dockerup.sh db status: healthy?"}
    S -->|yes| U["Fix [postgres] database_url"]
    S -->|no| V["./dockerup.sh db up, wait 30s, check again"]
```

## Contract DB commands

```sh
./dockerup.sh db status      # healthy?
./dockerup.sh db logs
./dockerup.sh db psql
./dockerup.sh db stop        # data kept
./dockerup.sh db up
```

Wipe every pushed day and start fresh:

```sh
docker compose -f docker/contract-postgres/docker-compose.yml down -v
./dockerup.sh db up
```

## After a code change

```sh
git pull
./dockerup.sh up      # rebuilds the image; the DB and its data stay
```

## Where things are

| Path | What |
|------|------|
| `v5-python/conf/` | `config.ini`, `keys.ini` (read-only in the container) |
| `data/YYYYMMDD/` | `raw/`, `normalized/`, `plugin/` |
| `v5-python/constituents/contracts/YYYYMMDD/` | basket contracts |
| `v5-python/bin/LOGS/` | one log per run |
| `v5-python/strategies/<NAME>/data/` | strategy output |
| `docker/Dockerfile` | the image |
| `docker/contract-postgres/` | the contract DB |

Without Docker (venv): [v5-python/docs/deploy/setup.markdown](../v5-python/docs/deploy/setup.markdown).
