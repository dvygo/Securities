#!/bin/sh
# Publish dist/<version>/ as GitHub Release v<version> on the release repository.
#
#   packaging/publish.sh NOTES.md [--draft]
#
# NOTES.md is the release's notes (the CHANGELOG section of the release repository). The tag
# is created on the release repository's main, which must already carry this version's
# deploy files. gh must be logged in with write access to that repository.
set -eu
cd "$(dirname "$0")/.."
REPO=${PREMARKET_RELEASE_REPO:-deshik-ux/securities}
notes=${1:?usage: packaging/publish.sh NOTES.md [--draft]}
draft=""
[ "${2:-}" = "--draft" ] && draft="--draft"
version=$(tr -d ' \n' < VERSION)
dist="dist/$version"
[ -f "$dist/SHA256SUMS" ] || { echo "publish.sh: $dist/SHA256SUMS missing; run packaging/release.sh first" >&2; exit 1; }
(cd "$dist" && sha256sum -c --quiet SHA256SUMS)
gh release create "v$version" -R "$REPO" --title "Securities $version" --notes-file "$notes" $draft \
    "$dist"/*.tar.gz "$dist/SHA256SUMS"
gh release view "v$version" -R "$REPO" --json url,assets -q '.url, (.assets[] | "  \(.name) \(.size)")'
