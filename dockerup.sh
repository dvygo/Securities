#!/bin/sh
# Builds the v5 pipeline image from this checkout and runs it, with the contract DB beside it:
#
#   securities:latest  docker/Dockerfile: Ubuntu 24.04, Python 3.12, the libraries pinned in
#                      packaging/requirements.lock, premarket and strategies compiled by Nuitka.
#                      The build fails if a unit test fails.
#   contract DB        docker/contract-postgres: Postgres 16 on port 6006 (container
#                      contractdb-contract-contract). Skipped when CONTRACT_DB=external.
#
# Runs are one-shot containers on the host network, as you, in the host's time zone. They use
# the same directories as `python -m premarket`: v5-python/conf/ (read-only), data/,
# v5-python/constituents/contracts/, v5-python/bin/LOGS/, v5-python/strategies/<NAME>/data/.
#
#   ./dockerup.sh up                  build the image, create the config files, start the contract DB
#   ./dockerup.sh india               premarket india
#   ./dockerup.sh normalize           premarket normalize                (files only, no DB)
#   ./dockerup.sh push                premarket normalize --only postgres
#   ./dockerup.sh daily               india, then normalize --postgres-push
#   ./dockerup.sh premarket ARGS...   any premarket command: xnas, xcme, xcbo, normalize --plugin, ...
#   ./dockerup.sh strategies ARGS...  strategies --strategy=str04
#   ./dockerup.sh check               config files, Databento keys, contract DB connection
#   ./dockerup.sh db up|stop|status|logs|psql
#   ./dockerup.sh down                stop the contract DB (data kept)
#   ./dockerup.sh status
#
# india, normalize, push and daily pass extra arguments on, e.g. ./dockerup.sh push --date-dir 20260918
set -eu
cd "$(dirname "$0")"
self=$(basename "$0")

IMAGE=${SECURITIES_IMAGE:-securities:latest}
DB="docker compose -f docker/contract-postgres/docker-compose.yml"
DB_CONTAINER=contractdb-contract-contract
CONF=$PWD/v5-python/conf
DATA=$PWD/data
CONTRACTS=$PWD/v5-python/constituents/contracts
LOGS=$PWD/v5-python/bin/LOGS

die() { echo "$*" >&2; exit 1; }

strategies() {
    for d in v5-python/strategies/*/loader.py; do
        [ -f "$d" ] && basename "$(dirname "$d")"
    done
}

# run PROGRAM ARGS...: one run of PROGRAM (premarket, strategies, python3) in the image.
run() {
    prog=$1
    shift
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no image $IMAGE; run ./dockerup.sh up"
    [ -f "$CONF/config.ini" ] || die "v5-python/conf/config.ini is missing; run ./dockerup.sh up"
    mkdir -p "$DATA" "$CONTRACTS" "$LOGS"
    # Options go in front of the program's arguments, so paths with spaces survive.
    set -- "$IMAGE" "$@"
    for s in $(strategies); do
        mkdir -p "v5-python/strategies/$s/data"
        set -- -v "$PWD/v5-python/strategies/$s/data:/opt/securities/strategies/$s/data" "$@"
    done
    for v in DATABASE_URL DATABASE_URL_PLUGIN DATABENTO_KEY_XNAS DATABENTO_KEY_XCBO DATABENTO_KEY_XCME; do
        eval "val=\${$v:-}"
        [ -z "$val" ] || set -- -e "$v" "$@"
    done
    { [ -t 0 ] && [ -t 1 ]; } && set -- -it "$@"
    docker run --rm --network host --user "$(id -u):$(id -g)" \
        -v /etc/localtime:/etc/localtime:ro \
        -v "$CONF:/opt/securities/conf:ro" \
        -v "$DATA:/var/lib/securities/data" \
        -v "$CONTRACTS:/var/lib/securities/contracts" \
        -v "$LOGS:/var/lib/securities/logs" \
        -e PREMARKET_DATA_ROOT=/var/lib/securities/data \
        -e PREMARKET_CONTRACTS_DIR=/var/lib/securities/contracts \
        -e PREMARKET_LOGS_DIR=/var/lib/securities/logs \
        -e DATABENTO_ENV="${DATABENTO_ENV:-production}" \
        --entrypoint "$prog" "$@"
}

db() {
    case "${1:-}" in
        up) $DB up -d; echo "contract DB: postgres://contract:contract@127.0.0.1:6006/contractdb (healthy after ~30s)" ;;
        stop) $DB stop ;;
        status) $DB ps ;;
        logs) docker logs -f --tail 100 "$DB_CONTAINER" ;;
        psql) t=-i; [ -t 0 ] && t=-it; docker exec $t "$DB_CONTAINER" psql -U contract -d contractdb ;;
        *) die "usage: ./dockerup.sh db up|stop|status|logs|psql" ;;
    esac
}

up() {
    docker build -f docker/Dockerfile -t "$IMAGE" .
    [ -f "$CONF/config.ini" ] || cp "$CONF/config.ini.example" "$CONF/config.ini"
    [ -f "$CONF/keys.ini" ] || (umask 077 && cp "$CONF/keys.ini.example" "$CONF/keys.ini")
    chmod 600 "$CONF/keys.ini"
    if [ "${CONTRACT_DB:-local}" = external ]; then
        echo "contract DB: external, from [postgres] database_url in v5-python/conf/config.ini"
    else
        db up
    fi
    echo "image: $IMAGE"
    echo "next:  ./dockerup.sh check"
}

# check: what a run needs, without printing a secret.
check() {
    for f in config.ini keys.ini; do
        [ -f "$CONF/$f" ] && echo "config:    v5-python/conf/$f" || echo "config:    v5-python/conf/$f MISSING"
    done
    env=${DATABENTO_ENV:-production}
    keys=$(awk -v s="[$env]" '/^\[/{ p = ($0 == s) } p && /^key_X[A-Z]+=./ { sub(/=.*/, ""); sub(/^key_/, ""); printf "%s ", $0 }' "$CONF/keys.ini" 2>/dev/null)
    echo "databento: [$env] keys for: ${keys:-none (India needs none; US venues and strategies do)}"
    run python3 -c '
import configparser, os, sys
import psycopg
cfg = configparser.ConfigParser()
cfg.read("/opt/securities/conf/config.ini")
url = os.getenv("DATABASE_URL") or cfg.get("postgres", "database_url", fallback="")
if not url:
    sys.exit("database:  no [postgres] database_url in config.ini")
where = url.rsplit("@", 1)[-1]
try:
    psycopg.connect(url, connect_timeout=5).close()
except Exception as e:
    sys.exit(f"database:  cannot connect to {where}: {str(e).splitlines()[0]}")
print(f"database:  ok ({where})")
'
}

status() {
    docker image inspect "$IMAGE" --format "image: $IMAGE built {{.Created}}" 2>/dev/null || echo "image: $IMAGE not built"
    $DB ps
}

case "${1:-}" in
    up) up ;;
    india) shift; run premarket india "$@" ;;
    normalize) shift; run premarket normalize "$@" ;;
    push) shift; run premarket normalize --only postgres "$@" ;;
    daily) shift; run premarket india "$@"; run premarket normalize --postgres-push "$@" ;;
    premarket | strategies) run "$@" ;;
    check) check ;;
    db) shift; db "$@" ;;
    down) $DB stop ;;
    status) status ;;
    *) sed -n '2,/^set -eu$/{/^set -eu$/!p}' "$self"; exit 2 ;;
esac
