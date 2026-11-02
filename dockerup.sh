#!/bin/sh
# Builds the pipeline image from this checkout and runs it:
#
#   securities:latest  docker/Dockerfile: Ubuntu 24.04, Python 3.12, the libraries pinned in
#                      packaging/requirements.lock, premarket compiled by Nuitka. The build
#                      fails if a unit test fails.
#
# Runs are one-shot containers on the host network, as you, in the host's time zone. They use
# the same directories as `python -m premarket` run from this checkout: conf/ (read-only),
# premarket/data/ (the days and the numbering state), constituents/contracts/, bin/LOGS/ and
# docs/QAT_GENERATED/. conf/config.ini's [paths] is not used.
#
#   ./dockerup.sh up                  build the image, create conf/config.ini and conf/keys.ini
#   ./dockerup.sh download            india, then xnas, xcme, xcbo (each with conf/symbols/<MIC>.txt if present)
#   ./dockerup.sh normalize           normalize XNAS, XCME, XCBO in that order, then XNSE, XBOM, XIMC;
#                                     then check-state
#   ./dockerup.sh push                premarket load, through every sink in conf/sinks/<name>.ini
#   ./dockerup.sh daily               download, normalize, push
#   ./dockerup.sh premarket ARGS...   any premarket command: init-state, check-tokens, normalize --dates, ...
#   ./dockerup.sh check               config files, Databento keys, sinks and their databases, numbering state
#   ./dockerup.sh db up|stop|status|logs clickhouse|postgres
#                                     the bundled databases: docker/contract-clickhouse (8744/7744),
#                                     docker/contract-postgres (6006). Neither is started unless asked.
#   ./dockerup.sh status
#
# download, normalize and push pass extra arguments on to each premarket command they run, e.g.
# ./dockerup.sh push --date-dir 20261030. Given arguments, normalize runs only
# `premarket normalize ARGS`.
set -eu
cd "$(dirname "$0")"
self=$(basename "$0")

IMAGE=${SECURITIES_IMAGE:-securities:latest}
CONF=$PWD/conf
DATA=$PWD/premarket/data
CONTRACTS=$PWD/constituents/contracts
LOGS=$PWD/bin/LOGS
QAT=$PWD/docs/QAT_GENERATED

US="XNAS XCME XCBO"
INDIA="XNSE XBOM XIMC"

die() { echo "$*" >&2; exit 1; }

# sinks: the name of every conf/sinks/<name>.ini, which is what push delivers through.
sinks() {
    for f in "$CONF"/sinks/*.ini; do
        [ -f "$f" ] && basename "$f" .ini
    done
    return 0
}

# sink_value NAME KEY: KEY's value in conf/sinks/NAME.ini's [sink].
sink_value() {
    sed -n "s/^$2[[:space:]]*=[[:space:]]*//p" "$CONF/sinks/$1.ini" | tail -n 1
}

# run PROGRAM ARGS...: one run of PROGRAM (premarket, python3) in the image.
run() {
    prog=$1
    shift
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no image $IMAGE; run ./dockerup.sh up"
    [ -f "$CONF/config.ini" ] || die "conf/config.ini is missing; run ./dockerup.sh up"
    mkdir -p "$DATA" "$CONTRACTS" "$LOGS" "$QAT"
    # Options go in front of the program's arguments, so paths with spaces survive.
    set -- "$IMAGE" "$@"
    # The mdf-tokenmap sink may deliver straight into MDF's config directory: mount it where
    # the sink file says it is.
    for s in $(sinks); do
        dir=$(sink_value "$s" dir)
        case $dir in /*) [ -d "$dir" ] && set -- -v "$dir:$dir" "$@" ;; esac
    done
    for v in DATABASE_URL DATABENTO_ENV DATABENTO_KEY_XNAS DATABENTO_KEY_XCBO DATABENTO_KEY_XCME; do
        eval "val=\${$v:-}"
        [ -z "$val" ] || set -- -e "$v" "$@"
    done
    for v in $(env | sed -n 's/^\(PREMARKET_SINK_[A-Z0-9_]*\)=.*/\1/p'); do
        set -- -e "$v" "$@"
    done
    { [ -t 0 ] && [ -t 1 ]; } && set -- -it "$@"
    docker run --rm --network host --user "$(id -u):$(id -g)" \
        -v /etc/localtime:/etc/localtime:ro \
        -v "$CONF:/opt/securities/conf:ro" \
        -v "$DATA:/var/lib/securities/data" \
        -v "$CONTRACTS:/var/lib/securities/contracts" \
        -v "$LOGS:/var/lib/securities/logs" \
        -v "$QAT:/var/lib/securities/qat" \
        -e PREMARKET_DATA_ROOT=/var/lib/securities/data \
        -e PREMARKET_CONTRACTS_DIR=/var/lib/securities/contracts \
        -e PREMARKET_LOGS_DIR=/var/lib/securities/logs \
        -e PREMARKET_QAT_DIR=/var/lib/securities/qat \
        --entrypoint "$prog" "$@"
}

download() {
    run premarket india "$@"
    for mic in $US; do
        if [ -f "$CONF/symbols/$mic.txt" ]; then
            run premarket "$(echo "$mic" | tr A-Z a-z)" --symbols-file "/opt/securities/conf/symbols/$mic.txt" "$@"
        else
            run premarket "$(echo "$mic" | tr A-Z a-z)" "$@"
        fi
    done
}

# normalize: the US venues one at a time, in the order new contracts are numbered, then India.
normalize() {
    if [ $# -gt 0 ]; then
        run premarket normalize "$@"
        return
    fi
    for mic in $US; do
        run premarket normalize --venue "$mic" --reason "dockerup normalize $mic"
    done
    set --
    for mic in $INDIA; do set -- "$@" --venue "$mic"; done
    run premarket normalize "$@" --reason "dockerup normalize $INDIA"
    run premarket check-state
}

push() {
    set -- $(for s in $(sinks); do printf -- '--sink %s ' "$s"; done) "$@"
    case " $* " in *" --sink "*) ;; *) die "no sinks: copy the ones to deliver to from conf/sinks/*.ini.example to conf/sinks/<name>.ini" ;; esac
    run premarket load "$@"
}

db() {
    case "${2:-}" in
        clickhouse | postgres) compose="docker compose -f docker/contract-$2/docker-compose.yml" ;;
        *) die "usage: ./dockerup.sh db up|stop|status|logs clickhouse|postgres" ;;
    esac
    case "${1:-}" in
        up) $compose up -d ;;
        stop) $compose stop ;;
        status) $compose ps ;;
        logs) $compose logs -f --tail 100 ;;
        *) die "usage: ./dockerup.sh db up|stop|status|logs clickhouse|postgres" ;;
    esac
}

up() {
    docker build -f docker/Dockerfile --build-arg BUILD_SHA="$(git rev-parse HEAD)" -t "$IMAGE" .
    [ -f "$CONF/config.ini" ] || cp "$CONF/config.ini.example" "$CONF/config.ini"
    [ -f "$CONF/keys.ini" ] || (umask 077 && cp "$CONF/keys.ini.example" "$CONF/keys.ini")
    chmod 600 "$CONF/keys.ini"
    mkdir -p "$DATA" "$CONTRACTS" "$LOGS" "$QAT"
    echo "image: $IMAGE"
    echo "sinks: $(sinks | tr '\n' ' ')"
    echo "next:  copy the sinks you deliver to (conf/sinks/*.ini.example), then ./dockerup.sh check"
}

# check: what a run needs, without printing a secret.
check() {
    for f in config.ini keys.ini; do
        [ -f "$CONF/$f" ] && echo "config:    conf/$f" || echo "config:    conf/$f MISSING"
    done
    env=${DATABENTO_ENV:-production}
    keys=$(awk -v s="[$env]" '/^\[/{ p = ($0 == s) } p && /^key_X[A-Z]+=./ { sub(/=.*/, ""); sub(/^key_/, ""); printf "%s ", $0 }' "$CONF/keys.ini" 2>/dev/null)
    echo "databento: [$env] keys for: ${keys:-none (India needs none; XNAS, XCME and XCBO do)}"
    symbols=$(cd "$CONF/symbols" 2>/dev/null && ls -- *.txt 2>/dev/null | tr '\n' ' ')
    echo "symbols:   ${symbols:-none (downloads take every symbol)}"
    run python3 -c '
import configparser, glob, os, sys
ok = True
for path in sorted(glob.glob("/opt/securities/conf/sinks/*.ini")):
    name = os.path.basename(path)[:-4]
    cfg = configparser.ConfigParser()
    cfg.read(path)
    s = cfg["sink"] if "sink" in cfg else {}
    kind = s.get("type", "?")
    try:
        if kind == "postgres-plugin":
            import psycopg
            url = s.get("database_url", "")
            where = url.rsplit("@", 1)[-1]
            psycopg.connect(url, connect_timeout=5).close()
        elif kind == "clickhouse-normal":
            import clickhouse_connect
            host, port, database = s.get("host"), s.get("port"), s.get("database")
            where = f"{host}:{port}/{database}"
            clickhouse_connect.get_client(host=s.get("host"), port=int(s.get("port")),
                                          username=s.get("username"), password=s.get("password"),
                                          database=s.get("database"), connect_timeout=5).ping()
        elif kind == "mdf-tokenmap":
            where = s.get("dir") or "the day tree"
            if s.get("dir") and not os.access(s.get("dir"), os.W_OK):
                raise OSError(f"cannot write {where}")
        else:
            where = "?"
        print(f"sink:      {name} ({kind}): ok ({where})")
    except Exception as e:
        ok = False
        print(f"sink:      {name} ({kind}): FAILED {str(e).splitlines()[0]}")
if not glob.glob("/opt/securities/conf/sinks/*.ini"):
    print("sink:      none (push needs at least one conf/sinks/<name>.ini)")
sys.exit(0 if ok else 1)
' || true
    if [ -f "$DATA/_state/manifest.json" ]; then
        run premarket check-state | tail -n 3
    else
        echo "state:     none yet: ./dockerup.sh premarket init-state --dry-run --reason \"first run on this host\""
    fi
}

status() {
    docker image inspect "$IMAGE" --format "image: $IMAGE built {{.Created}}" 2>/dev/null || echo "image: $IMAGE not built"
    for d in clickhouse postgres; do docker compose -f "docker/contract-$d/docker-compose.yml" ps; done
}

case "${1:-}" in
    up) up ;;
    download) shift; download "$@" ;;
    normalize) shift; normalize "$@" ;;
    push) shift; push "$@" ;;
    daily) shift; download "$@"; normalize; push "$@" ;;
    premarket) run "$@" ;;
    check) check ;;
    db) shift; db "$@" ;;
    status) status ;;
    *) sed -n '2,/^set -eu$/{/^set -eu$/!p}' "$self"; exit 2 ;;
esac
