#!/bin/sh
# Build the release: dist/<version>/
#   securities_<v>_ubuntu22.04_amd64.tar.gz        libexec/ libs/ share/ install.sh + licenses, what hosts run
#   securities_<v>_ubuntu22.04_amd64.debug.tar.gz  premarket.debug, for gdb against a core
#   SHA256SUMS
#
#   packaging/release.sh            from a clean tree at HEAD
#   packaging/release.sh --dirty    allow uncommitted changes (local trials; BUILD_SHA says so)
#
# Everything compiles and every test runs inside securities-build:22.04. libs/ gets every
# library the program loads except glibc's own, copied from the image it was linked in. The
# finished tarball then goes through packaging/smoke.sh on a bare ubuntu:22.04.
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
    rm -rf $B/stage && mkdir -p $S/libexec $S/libs $S/share/conf/sinks $S/share/conf/symbols $S/share/baskets
    objcopy --only-keep-debug $B/premarket $B/premarket.debug
    cp $B/premarket $S/libexec/premarket
    strip --strip-all $S/libexec/premarket
    objcopy --add-gnu-debuglink=$B/premarket.debug $S/libexec/premarket
    ldd $B/premarket | sed -n "s|^[[:space:]]*\([^ ]*\) => \(/[^ ]*\) .*|\1 \2|p" | while read -r so path; do
        case $so in
            ld-linux* | libc.so.* | libm.so.* | libdl.so.* | libpthread.so.* | librt.so.* | libresolv.so.* | libutil.so.*) ;;
            *) cp -L "$path" "$S/libs/$so" ;;
        esac
    done
    ls $S/libs
    cp conf/config.ini.example conf/keys.ini.example $S/share/conf/
    cp conf/sinks/*.ini.example $S/share/conf/sinks/
    for f in conf/symbols/*.txt; do cp "$f" "$S/share/conf/symbols/$(basename "$f").example"; done
    cp -r constituents/baskets/. $S/share/baskets/
    install -m0755 packaging/install.sh $S/install.sh
    cp LICENSE $S/
    printf "%s\n" "$SHA" > $S/BUILD_SHA
    cp VERSION $S/
    sh packaging/notices.sh $S/THIRD_PARTY_NOTICES.txt $S/libs
    cd $B/stage
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@$EPOCH -cf - $NAME | gzip -n -9 > /src/build/release/$NAME.tar.gz
    mkdir -p dbg && cp $B/premarket.debug dbg/premarket.debug
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@$EPOCH -C dbg -cf - premarket.debug | gzip -n -9 > /src/build/release/$NAME.debug.tar.gz
'
mv build/release/"$name".tar.gz build/release/"$name".debug.tar.gz "$dist"/
(cd "$dist" && sha256sum "$name".tar.gz "$name".debug.tar.gz > SHA256SUMS)

echo "smoke test on a bare ubuntu:22.04"
docker run --rm -v "$PWD/$dist":/dist:ro -v "$PWD/tests/data":/fixture:ro -v "$PWD/packaging/smoke.sh":/smoke.sh:ro \
    ubuntu:22.04 bash /smoke.sh "$name"
ls -la "$dist"
