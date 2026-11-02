#!/bin/sh
# Builds the release artifacts for VERSION (default: pyproject.toml's version) into
# dist/<version>/:
#
#   securities_<v>_ubuntu24.04_amd64.tar.gz   premarket.bin (compiled with Nuitka), its launcher
#                                             in bin/, BUILD_SHA, the conf templates and symbol
#                                             lists, the baskets, requirements.lock, LICENSE and
#                                             THIRD_PARTY_NOTICES.txt: /opt/securities of the image
#   securities-images_<v>_linux_amd64.tar.gz  the securities:<v> image, for `docker load`
#   SHA256SUMS
#
# Everything comes out of docker/Dockerfile, whose build stage runs the unit tests against
# the pinned libraries before it compiles. Neither artifact carries .py or .pyc of this repo.
#
#   packaging/release.sh [VERSION]
set -eu
cd "$(dirname "$0")/.."

pyver=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)
pkgver=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' premarket/__init__.py)
version=${1:-$pyver}
version=${version#v}
[ "$version" = "$pyver" ] || { echo "pyproject.toml is $pyver, not $version" >&2; exit 1; }
[ "$pkgver" = "$pyver" ] ||
    { echo "premarket/__init__.py says $pkgver but pyproject.toml says $pyver" >&2; exit 1; }
# Every module in premarket/ is compiled in, tracked or not, and the image and the release
# notes name the commit. So build only what is committed.
src="premarket conf constituents tests pyproject.toml requirements.txt requirements.lock LICENSE packaging docker .dockerignore"
if [ -n "$(git status --porcelain -- $src)" ]; then
    echo "uncommitted or untracked files in the build; commit or remove them:" >&2
    git status --short -- $src >&2
    exit 1
fi

out=dist/$version
stage=$out/stage
# The image label names the license the LICENSE file grants.
case $(head -c 300 LICENSE) in
    *"Apache License"*"Version 2.0"*) license=Apache-2.0 ;;
    *"BSD 3-Clause"*) license=BSD-3-Clause ;;
    *) echo "LICENSE: not Apache 2.0 or BSD 3-Clause; name it in packaging/release.sh" >&2; exit 1 ;;
esac
commit=$(git rev-parse --short=12 HEAD)
sha=$(git rev-parse HEAD)
image=securities:$version

docker build -f docker/Dockerfile --build-arg BUILD_SHA="$sha" -t "$image" \
    --label org.opencontainers.image.title=securities \
    --label org.opencontainers.image.version="$version" \
    --label org.opencontainers.image.revision="$commit" \
    --label org.opencontainers.image.licenses="$license" .

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
