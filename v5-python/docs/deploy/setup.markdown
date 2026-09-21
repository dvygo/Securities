# Premarket v5 — Setup

One-time setup for a new machine. Do this once, then use the runbook every day.

All commands run from the `v5-python` folder.

```
cd Securities/v5-python
```

## What you need first

- Python 3.10 or newer (`python3 --version`)
- Docker with the compose plugin on the DB host (`docker compose version`)
- Network access from this machine to the DB host on port 6006
- The Databento API keys, if you will run the US venues (xnas / xcme / xcbo). India does not need a key.

## 1. Create the venv

```
python3 -m venv .venv
```

This makes a `.venv` folder inside `v5-python`. Do it once. Never install the libraries into the system Python.

## 2. Activate it

```
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`. You have to do this in every new terminal before running anything.

To leave the venv later: `deactivate`.

## 3. Install the libraries

With the venv active:

```
pip install --upgrade pip
pip install -r requirements.txt
```

Check it worked:

```
python -m premarket --help
```

You should see the list of commands: `india`, `xcme`, `xcbo`, `xnas`, `normalize`.

## 4. Make the config files

Both live in `conf/`. Copy the examples:

```
cp conf/config.ini.example conf/config.ini
cp conf/keys.ini.example conf/keys.ini
```

These two files are gitignored. They hold passwords and keys, so don't commit them and don't send them around.

## 5. Spin up the contract DB

Do this on the machine that will host the database. That can be this machine or a separate DB server. The compose file ships in `v5-python/docker/`.

```
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

This starts Postgres 16 in a container named `contractdb-contract-contract`:

- Port: `6006` on the host
- Database: `contractdb`
- User / password: `contract` / `contract`
- Data is kept in the Docker volume `contract_postgres_data`. It survives restarts and reboots, and the container comes back up on its own.

Check it's up and healthy:

```
docker compose -f docker/contract-postgres/docker-compose.yml ps
```

The status should say `healthy`. It takes up to 30 seconds after first start.

To open psql inside it:

```
docker exec -it contractdb-contract-contract psql -U contract -d contractdb
```

Other commands you might need:

| What | Command |
|---|---|
| Stop it (data kept) | `docker compose -f docker/contract-postgres/docker-compose.yml stop` |
| Start it again | `docker compose -f docker/contract-postgres/docker-compose.yml up -d` |
| Logs | `docker logs contractdb-contract-contract` |
| **Wipe everything and start fresh** | `docker compose -f docker/contract-postgres/docker-compose.yml down -v` then `up -d` |

`down -v` deletes every pushed day. Only use it when you mean to.

If this DB is reachable from outside the box, change the `contract` / `contract` login. Do it before the first `up -d`, in `POSTGRES_USER` and `POSTGRES_PASSWORD` in the compose file. Once the volume exists, changing the compose file doesn't change the login.

## 6. Point Postgres at the contract DB

Open `conf/config.ini` and find the `[postgres]` section:

```
[postgres]
database_url = postgres://contract:contract@127.0.0.1:6006/contractdb?sslmode=disable
```

If the DB runs on this same machine with the default login, leave it as is.

If it runs on another server, or you changed the login, set:

```
database_url = postgres://USER:PASSWORD@HOST:PORT/contractdb?sslmode=disable
```

- `USER` / `PASSWORD`: the contract DB login
- `HOST` / `PORT`: the DB server's address and `6006`
- If the password has special characters (`@ : / ? #`), URL-encode them. For example, `@` becomes `%40`.

That DB user must be allowed to create schemas and tables. Each push creates `v4_YYYYMMDD` and `v4_YYYYMMDD_baskets` and drops and recreates the `contracts` and `baskets` tables in them and in `public`.

Only if you use the plugin push: set `[postgres-plugin] database_url` the same way. Its table must already exist (see `docs/plugin/pg_data_types.txt`).

## 7. Databento keys (US venues only)

Open `conf/keys.ini` and fill in the `[production]` section:

```
[production]
key_XNAS=db-xxxxxxxx
key_XCBO=db-xxxxxxxx
key_XCME=db-xxxxxxxx
```

Leave `[development]` empty unless you've been told to use it.

## 8. Leave the rest alone

The other sections (`[paths]`, `[databento]`, `[normalizer]`, `[fyers]`) work as shipped. The only one you might touch is `[paths] data_dir`. It's where the daily data lands and defaults to `../data`, which is `Securities/data`.

## 9. Test the database connection

```
python -c "import psycopg; from premarket import config; psycopg.connect(config.database_url()).close(); print('db ok')"
```

`db ok` means you're done. If it fails, recheck host, port, user and password in `[postgres]`. Also check that step 5 shows the container `healthy` and that this machine can reach the server on port 6006.

## Overrides (optional)

Environment variables take priority over the ini files:

| Variable | Overrides |
|---|---|
| `DATABASE_URL` | `[postgres] database_url` |
| `DATABASE_URL_PLUGIN` | `[postgres-plugin] database_url` |
| `DATABENTO_KEY_XNAS` / `_XCBO` / `_XCME` | keys in `keys.ini` |
| `DATABENTO_ENV` | which `keys.ini` section is used (default `production`) |
| `PREMARKET_CONFIG` | path to `config.ini` |
| `PREMARKET_KEYS` | path to `keys.ini` |
