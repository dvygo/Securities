#!/bin/sh
# Launcher for a compiled program of the Securities release: bin/<name> runs <root>/<name>.bin.
# It points PREMARKET_V5_ROOT at <root> (unless already set), which is where premarket looks
# for conf/ and constituents/. Outside the image, the pinned libraries go in <root>/.venv
# (python3 -m venv .venv && .venv/bin/pip install -r requirements.lock).
set -eu
self=$(readlink -f "$0")
root=$(dirname "$(dirname "$self")")
: "${PREMARKET_V5_ROOT:=$root}"
export PREMARKET_V5_ROOT
for sp in "$root"/.venv/lib/python3.12/site-packages; do
    [ -d "$sp" ] && export PYTHONPATH="$sp${PYTHONPATH:+:$PYTHONPATH}"
done
exec "$root/$(basename "$self").bin" "$@"
