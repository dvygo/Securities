#!/bin/sh
# Builds the release artifacts for VERSION (default: v5-python/pyproject.toml's version) into
# dist/<version>/:
#
#   securities_<v>_ubuntu24.04_amd64.tar.gz   premarket.bin and strategies.bin (compiled with
#                                             Nuitka), their launchers in bin/, the conf
#                                             templates, basket templates, strategy underlyings,
#                                             requirements.lock, LICENSE and
#                                             THIRD_PARTY_NOTICES.txt: /opt/securities of the image
#   securities-images_<v>_linux_amd64.tar.gz  the securities:<v> image, for `docker load`
#   SHA256SUMS
#
# Everything comes out of packaging/Dockerfile, whose build stage runs the unit tests against
# the pinned libraries before it compiles. Neither artifact carries .py or .pyc of this repo.
#
#   packaging/release.sh [VERSION]
set -eu
cd "$(dirname "$0")/.."

pyver=$(sed -n 's/^version = "\(.*\)"$/\1/p' v5-python/pyproject.toml)
pkgver=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' v5-python/premarket/__init__.py)
version=${1:-$pyver}
version=${version#v}
[ "$version" = "$pyver" ] || { echo "v5-python/pyproject.toml is $pyver, not $version" >&2; exit 1; }
[ "$pkgver" = "$pyver" ] ||
    { echo "premarket/__init__.py says $pkgver but pyproject.toml says $pyver" >&2; exit 1; }
# Every module in premarket/ and strategies/ is compiled in, tracked or not, and the release
# notes name the commit. So build only what is committed.
if [ -n "$(git status --porcelain -- v5-python packaging .dockerignore)" ]; then
    echo "uncommitted or untracked files under v5-python/ or packaging/; commit or remove them:" >&2
    git status --short -- v5-python packaging .dockerignore >&2
    exit 1
fi

out=dist/$version
stage=$out/stage
commit=$(git rev-parse --short=12 HEAD)
image=securities:$version

docker build -f packaging/Dockerfile -t "$image" \
    --label org.opencontainers.image.title=securities \
    --label org.opencontainers.image.version="$version" \
    --label org.opencontainers.image.revision="$commit" \
    --label org.opencontainers.image.licenses=BSD-3-Clause .

rm -rf "$out"
mkdir -p "$stage"

d=securities_${version}_ubuntu24.04_amd64
id=$(docker create "$image")
docker cp -q "$id:/opt/securities" "$stage/$d"
docker rm "$id" >/dev/null
if find "$stage/$d" -name '*.py' -o -name '*.pyc' | grep .; then
    echo "Python sources in the tarball" >&2
    exit 1
fi
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$stage" -czf "$out/$d.tar.gz" "$d"

docker save "$image" | gzip > "$out/securities-images_${version}_linux_amd64.tar.gz"

rm -rf "$stage"
(cd "$out" && sha256sum -- *.tar.gz > SHA256SUMS)
ls -l "$out"
