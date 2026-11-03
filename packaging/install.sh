#!/bin/sh
# Install this premarket release on an Ubuntu 22.04 host. Run it from the unpacked tarball:
#
#   sha256sum -c SHA256SUMS --ignore-missing
#   tar -xzf securities_<v>_ubuntu22.04_amd64.tar.gz
#   sudo securities_<v>_ubuntu22.04_amd64/install.sh [--prefix /opt/securities] [--owner USER]
#
#   <prefix>/<v>/       this release: bin/premarket, lib/libduckdb.so, share/
#   <prefix>/current    symlink to the release in use, switched only after the new one runs
#   <prefix>/conf/      config.ini, keys.ini, sinks/, symbols/: seeded once, never overwritten
#   <prefix>/state/     data/ (the numbering state is data/_state/), contracts/, logs/, reports/
#
# Installing again, or installing the next version, leaves conf/ and state/ as they are.
# --owner USER hands conf/ and state/ to the account that will run premarket.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
prefix=/opt/securities
owner=
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) prefix=$2; shift 2 ;;
        --prefix=*) prefix=${1#--prefix=}; shift ;;
        --owner) owner=$2; shift 2 ;;
        --owner=*) owner=${1#--owner=}; shift ;;
        -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$prefix" in
    /*) ;;
    *) echo "install.sh: --prefix must be an absolute path, got $prefix" >&2; exit 2 ;;
esac
version=$(cat "$here/VERSION")

[ -e /usr/share/zoneinfo/Asia/Kolkata ] || { echo "install.sh: no tz database; apt-get install tzdata" >&2; exit 1; }
"$here/bin/premarket" --version >/dev/null || { echo "install.sh: bin/premarket does not run on this host" >&2; exit 1; }

mkdir -p "$prefix/conf/sinks" "$prefix/conf/symbols" \
    "$prefix/state/data" "$prefix/state/contracts" "$prefix/state/logs" "$prefix/state/reports"

if [ "$here" != "$prefix/$version" ]; then
    rm -rf "$prefix/.$version.new"
    cp -a "$here" "$prefix/.$version.new"
    rm -rf "$prefix/$version"
    mv "$prefix/.$version.new" "$prefix/$version"
fi

seed() {
    if [ -e "$2" ]; then
        echo "  kept    $2"
    else
        cp "$1" "$2"
        echo "  created $2"
    fi
}
if [ ! -e "$prefix/conf/config.ini" ]; then
    sed "s|/opt/securities|$prefix|g" "$here/share/conf/config.ini.example" > "$prefix/conf/config.ini"
    echo "  created $prefix/conf/config.ini"
else
    echo "  kept    $prefix/conf/config.ini"
fi
seed "$here/share/conf/keys.ini.example" "$prefix/conf/keys.ini"
chmod 600 "$prefix/conf/keys.ini"
for f in "$here"/share/conf/symbols/*.txt.example; do
    seed "$f" "$prefix/conf/symbols/$(basename "$f" .example)"
done
for f in "$here"/share/conf/sinks/*.ini.example; do
    cp "$f" "$prefix/conf/sinks/"
done
if [ -n "$owner" ]; then
    chown -R "$owner" "$prefix/conf" "$prefix/state"
fi

ln -sfn "$version" "$prefix/.current.new"
mv -T "$prefix/.current.new" "$prefix/current"
"$prefix/current/bin/premarket" --version

cat <<EOF

Installed: $prefix/current -> $version
  binary   $prefix/current/bin/premarket
  config   $prefix/conf/config.ini, keys.ini (the Databento keys), sinks/<name>.ini
  state    $prefix/state/

Every command takes both files, by full path:
  $prefix/current/bin/premarket --config=$prefix/conf/config.ini --keys=$prefix/conf/keys.ini check-state

A new host runs init-state once; a host coming from 7.x runs migrate-state once instead.
EOF
