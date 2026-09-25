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
| [`v6-python/`](v6-python/) | **active** | The pipeline. CLI: `python -m premarketv6 {india,xcme,xcbo,xnas,normalize,load,init-state,check-tokens,check-lineage,check-state}`. See [`v6-python/README.md`](v6-python/README.md). |
| `docker/contract-postgres/` | shared | Postgres 16 container the pipeline pushes into (`docker compose -f docker/contract-postgres/docker-compose.yml up -d`). |

`v6-python/` is the only pipeline. The earlier implementations (`v5-python/`,
`v4-golang/`, and the v3 equity-algo scratch scripts) were removed in 6.3.0 and
remain in git history.

## Quick start (v6-python)

```bash
cd v6-python
python -m venv .venv && .venv/bin/pip install -e .[dev]

cp conf/config.ini.example conf/config.ini          # [EXCHANGE:*] venue ids; keys in conf/keys.ini
for f in conf/sinks/*.ini.example; do cp "$f" "${f%.example}"; done   # load targets
python -m premarketv6 init-state --reason "first run on this host"   # once per host

python -m premarketv6 xcme --all-symbols --today      # 1. download
python -m premarketv6 india
python -m premarketv6 normalize                       # 2. normalize (files only)
python -m premarketv6 load --sink clickhouse-normal --sink mdf-tokenmap   # 3. load
```

The pipeline is download -> normalize -> load: normalize never touches a
database, and every push is a named sink in `conf/sinks/`. The venues publish at
different times of day -- a full day is not available before roughly 16:30 IST,
because OPRA lands last -- so normalize is designed to be run repeatedly and to
leave already-numbered instruments untouched. Older days are filled with
`normalize --dates`. Details, including the token design, the numbering state
and the validation commands, are in [`v6-python/README.md`](v6-python/README.md).

Config lives in `conf/config.ini` (gitignored — never commit real API keys).
Basket templates live in `constituents/baskets/`.

## Database

The pipeline pushes into a local Postgres:

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
