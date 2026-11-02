#!/bin/sh
# Launcher for the compiled program of the Securities release: bin/premarket runs
# <root>/premarket.bin. Unless already set, it points PREMARKET_ROOT at <root>, which is
# where premarket looks for conf/ and constituents/, and PREMARKET_BUILD_SHA at the commit
# in <root>/BUILD_SHA, which every manifest records. Outside the image, the pinned libraries
# go in <root>/.venv (python3 -m venv .venv && .venv/bin/pip install -r requirements.lock).
set -eu
self=$(readlink -f "$0")
root=$(dirname "$(dirname "$self")")
: "${PREMARKET_ROOT:=$root}"
export PREMARKET_ROOT
if [ -z "${PREMARKET_BUILD_SHA:-}" ] && [ -f "$root/BUILD_SHA" ]; then
    PREMARKET_BUILD_SHA=$(cat "$root/BUILD_SHA")
    export PREMARKET_BUILD_SHA
fi
for sp in "$root"/.venv/lib/python3.12/site-packages; do
    [ -d "$sp" ] && export PYTHONPATH="$sp${PYTHONPATH:+:$PYTHONPATH}"
done
exec "$root/$(basename "$self").bin" "$@"
