#!/bin/sh
# Build the release: dist/<version>/
#   securities_<v>_ubuntu22.04_amd64.tar.gz        bin/ lib/ share/ install.sh + licenses, what hosts run
#   securities_<v>_ubuntu22.04_amd64.debug.tar.gz  premarket.debug, for gdb against a core
#   SHA256SUMS
#
#   packaging/release.sh            from a clean tree at HEAD
#   packaging/release.sh --dirty    allow uncommitted changes (local trials; BUILD_SHA says so)
#
# Everything compiles and every test runs inside securities-build:22.04. The finished tarball
# is then installed with its own install.sh on a bare ubuntu:22.04 with nothing added but
# tzdata, where it numbers a day of the test fixture end to end and migrates the 7.x fixture.
set -eu
cd "$(dirname "$0")/.."
IMAGE=securities-build:22.04
dirty=0
[ "${1:-}" = "--dirty" ] && dirty=1

version=$(tr -d ' \n' < VERSION)
sha=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain)" ]; then
    [ "$dirty" = 1 ] || { echo "release.sh: the tree has uncommitted changes (use --dirty for a trial)" >&2; exit 1; }
    sha="$sha-dirty"
fi
name="securities_${version}_ubuntu22.04_amd64"
dist="dist/$version"
rm -rf "$dist" && mkdir -p "$dist" build/release .cache/ccache
epoch=$(git log -1 --format=%ct)

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/build.Dockerfile -t "$IMAGE" docker/
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e CCACHE_DIR=/src/.cache/ccache -e TZ=Asia/Kolkata \
    -e NAME="$name" -e SHA="$sha" -e EPOCH="$epoch" -v "$PWD":/src -w /src "$IMAGE" sh -euc '
    B=/src/build/release
    cmake -S /src -B $B -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_CXX_FLAGS="-stdlib=libc++ -gdwarf-4" \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache -DPREMARKET_BUILD_SHA="$SHA" >/dev/null
    ninja -C $B
    $B/premarket_tests
    S=$B/stage/$NAME
    rm -rf $B/stage && mkdir -p $S/bin $S/lib $S/share/conf/sinks $S/share/conf/symbols $S/share/baskets
    objcopy --only-keep-debug $B/premarket $B/premarket.debug
    cp $B/premarket $S/bin/premarket
    strip --strip-all $S/bin/premarket
    objcopy --add-gnu-debuglink=$B/premarket.debug $S/bin/premarket
    cp /usr/local/lib/libduckdb.so $S/lib/
    cp conf/config.ini.example conf/keys.ini.example $S/share/conf/
    cp conf/sinks/*.ini.example $S/share/conf/sinks/
    for f in conf/symbols/*.txt; do cp "$f" "$S/share/conf/symbols/$(basename "$f").example"; done
    cp -r constituents/baskets/. $S/share/baskets/
    install -m0755 packaging/install.sh $S/install.sh
    cp LICENSE $S/
    printf "%s\n" "$SHA" > $S/BUILD_SHA
    cp VERSION $S/
    sh packaging/notices.sh $S/THIRD_PARTY_NOTICES.txt
    cd $B/stage
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@$EPOCH -cf - $NAME | gzip -n -9 > /src/build/release/$NAME.tar.gz
    mkdir -p dbg && cp $B/premarket.debug dbg/premarket.debug
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@$EPOCH -C dbg -cf - premarket.debug | gzip -n -9 > /src/build/release/$NAME.debug.tar.gz
'
mv build/release/"$name".tar.gz build/release/"$name".debug.tar.gz "$dist"/
(cd "$dist" && sha256sum "$name".tar.gz "$name".debug.tar.gz > SHA256SUMS)

echo "smoke test on a bare ubuntu:22.04"
docker run --rm -v "$PWD/$dist":/dist:ro -v "$PWD/tests/data":/fixture:ro ubuntu:22.04 bash -euo pipefail -c "
    apt-get update -qq >/dev/null && apt-get install -y -qq tzdata >/dev/null 2>&1
    cd /tmp && tar -xzf /dist/$name.tar.gz
    if ldd $name/bin/premarket | grep 'not found'; then exit 1; fi
    ./$name/install.sh --prefix /opt/securities >/dev/null
    PM='/opt/securities/current/bin/premarket --config=/opt/securities/conf/config.ini --keys=/opt/securities/conf/keys.ini'
    \$PM --version
    D=/opt/securities/state/data
    mkdir -p \$D/20261103 && cp -r /fixture/state7x/20261103/X* \$D/20261103/
    \$PM init-state --reason 'release smoke test' >/dev/null
    for v in XNSE XBOM XIMC; do \$PM normalize --venue \$v --date-dir 20261103 --reason 'release smoke test' >/dev/null; done
    for c in check-state 'check-tokens --venue XNSE --dates 20261103' 'check-lineage --venue XNSE --dates 20261103'; do out=\$(\$PM \$c | grep 'check(s):'); echo \"\$c: \$out\"; done
    mkdir -p /tmp/m && cp -r /fixture/state7x /tmp/m/data
    sed 's|^data_dir .*|data_dir = /tmp/m/data|' /opt/securities/conf/config.ini > /tmp/m/config.ini
    M='/opt/securities/current/bin/premarket --config=/tmp/m/config.ini'
    \$M migrate-state --reason 'release smoke test' | tail -1
    for v in XNSE XBOM XIMC; do \$M normalize --venue \$v --date-dir 20261103 --reason 'release smoke test' >/dev/null; done
    out=\$(\$M check-state | grep 'check(s):'); echo \"after migrate-state, check-state: \$out\""
ls -la "$dist"
