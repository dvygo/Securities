#!/bin/sh
# Publishes dist/<version>/ as GitHub Release v<version> in the private release repo.
# The notes are that version's section of the release repo's CHANGELOG.md, so write it
# there first. Re-running replaces the assets and notes of an existing release.
# gh must be able to write to the release repo (`gh auth login` as deshik-ux).
#
#   packaging/publish.sh [--check] [VERSION]    --check: only verify the changelog section exists
set -eu
cd "$(dirname "$0")/.."

repo=${RELEASE_REPO:-deshik-ux/securities}
check=false
[ "${1:-}" = --check ] && { check=true; shift; }
version=${1:-$(sed -n 's/^version = "\(.*\)"$/\1/p' v5-python/pyproject.toml)}
version=${version#v}
tag=v$version
dir=dist/$version

notes=$(mktemp)
trap 'rm -f "$notes"' EXIT
# Lines under "## [<version>]" up to the next "## " heading or the link references.
gh api "repos/$repo/contents/CHANGELOG.md" -H "Accept: application/vnd.github.raw" |
    awk -v v="$version" '/^## /{ p = index($0, "## [" v "]") == 1; next } /^\[[^]]*\]: /{ p = 0 } p' > "$notes"
if ! grep -q '[^[:space:]]' "$notes"; then
    echo "$repo CHANGELOG.md has no \"## [$version]\" section; add it before releasing $tag" >&2
    exit 1
fi
$check && exit 0

[ -f "$dir/SHA256SUMS" ] || { echo "no artifacts in $dir; run packaging/release.sh $version first" >&2; exit 1; }
(cd "$dir" && sha256sum --quiet -c SHA256SUMS)
printf '\n---\nBuild `%s` · image `securities:%s`\n' "$(git rev-parse --short=12 HEAD)" "$version" >> "$notes"

if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
    gh release upload "$tag" -R "$repo" --clobber "$dir"/*.tar.gz "$dir/SHA256SUMS"
    gh release edit "$tag" -R "$repo" --notes-file "$notes"
else
    gh release create "$tag" -R "$repo" --title "Securities $version" --notes-file "$notes" \
        "$dir"/*.tar.gz "$dir/SHA256SUMS"
fi
gh release view "$tag" -R "$repo" --json url --jq .url
