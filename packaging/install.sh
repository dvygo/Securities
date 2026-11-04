#!/bin/sh
# Install this premarket release into a directory you choose, on an Ubuntu 22.04 host. Always
# through sudo, from the unpacked tarball:
#
#   sha256sum -c SHA256SUMS --ignore-missing
#   tar -xzf securities_<v>_ubuntu22.04_amd64.tar.gz
#   sudo securities_<v>_ubuntu22.04_amd64/install.sh --prefix DIR [--owner USER]
#
#   DIR/bin/premarket       what you run: sets LD_LIBRARY_PATH=DIR/libs, then starts libexec/premarket
#   DIR/libexec/premarket   the program
#   DIR/libs/               every library it needs beyond glibc
#   DIR/share/              the config, keys, sink and symbol templates; the basket definitions
#   DIR/conf/               config.ini, keys.ini, sinks/, symbols/: seeded once, never overwritten
#   DIR/state/              data/ (the numbering state is data/_state/), contracts/, logs/, reports/
#
# The program files belong to root. conf/ and state/ belong to --owner USER: by default the account
# that ran sudo, and on a reinstall whoever owns conf/ already. Installing again, or the next version,
# into the same DIR replaces bin/, libexec/, libs/ and share/ and leaves conf/ and state/ as they are.
# It refuses while a premarket run holds the numbering lock.
set -eu
umask 022

die() {
    printf '\n*** install.sh FAILED: %s\n\n' "$1" >&2
    exit 1
}

here=$(cd "$(dirname "$0")" && pwd)
prefix=
owner=
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"; prefix=$2; shift 2 ;;
        --prefix=*) prefix=${1#--prefix=}; shift ;;
        --owner) [ $# -ge 2 ] || die "--owner needs a user"; owner=$2; shift 2 ;;
        --owner=*) owner=${1#--owner=}; shift ;;
        -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

if [ "$(id -u)" -ne 0 ] || [ -z "${SUDO_USER:-}" ]; then
    die "install.sh must be run with sudo, always:

        sudo $0 --prefix DIR [--owner USER]
"
fi

[ -n "$prefix" ] || die "--prefix DIR is required: the directory to install into (there is no default)"
case "$prefix" in
    /*) ;;
    *) die "--prefix must be an absolute path, got $prefix" ;;
esac
prefix=${prefix%/}
case "$prefix" in
    "" | *[!A-Za-z0-9._/-]* | */. | */./* | */.. | */../* | *//*)
        die "--prefix ${prefix:-/}: use a plain absolute path of letters, digits, . _ - and /, not / itself" ;;
esac
case "$prefix/" in
    "$here"/*) die "--prefix $prefix is inside the unpacked tarball; install somewhere else" ;;
esac

version=$(cat "$here/VERSION")
[ -e /usr/share/zoneinfo/Asia/Kolkata ] || die "no tz database on this host: apt-get install tzdata"

layout=new
if [ -e "$prefix" ] && [ ! -d "$prefix" ]; then
    die "$prefix exists and is not a directory"
elif [ -x "$prefix/libexec/premarket" ] && [ -f "$prefix/VERSION" ]; then
    layout=installed
elif [ -L "$prefix/current" ] && [ -x "$prefix/current/bin/premarket" ]; then
    layout=8.0
elif [ -d "$prefix" ] && [ -n "$(ls -A "$prefix")" ]; then
    die "$prefix is not empty and holds no premarket install; choose a new or empty directory"
fi

if [ -z "$owner" ]; then
    if [ -d "$prefix/conf" ]; then owner=$(stat -c %U "$prefix/conf"); else owner=$SUDO_USER; fi
fi
id -u "$owner" >/dev/null 2>&1 || die "--owner $owner: no such user"
[ "$(id -u "$owner")" -ne 0 ] ||
    die "conf/ and state/ would belong to root, and premarket is not run as root: run sudo from the account that runs premarket, or pass --owner USER"
group=$(id -gn "$owner")

data_dir=$prefix/state/data
if [ -f "$prefix/conf/config.ini" ]; then
    d=$(sed -n 's/^[[:space:]]*data_dir[[:space:]]*=[[:space:]]*\([^[:space:]#;]*\).*/\1/p' "$prefix/conf/config.ini" | head -n 1)
    [ -z "$d" ] || data_dir=$d
fi
lock=$data_dir/_state/.lock
if [ -e "$lock" ]; then
    exec 9<"$lock"
    flock -n 9 || die "a premarket run holds $lock ($(cat "$lock")); install when it has finished"
fi

mkdir -p "$prefix"
stage=$prefix/.install.new
old=$prefix/.install.old
rm -rf "$stage" "$old"
mkdir "$stage" "$stage/bin"
cp -R "$here/libexec" "$here/libs" "$here/share" "$stage/"
cp "$here/VERSION" "$here/BUILD_SHA" "$here/LICENSE" "$here/THIRD_PARTY_NOTICES.txt" "$stage/"
cat > "$stage/bin/premarket" <<EOF
#!/bin/sh
# premarket $version, installed in $prefix by install.sh: the program is libexec/premarket and
# every library it needs beyond glibc is in libs/.
LD_LIBRARY_PATH=$prefix/libs
export LD_LIBRARY_PATH
exec $prefix/libexec/premarket "\$@"
EOF
chown -R root:root "$stage"
find "$stage" -type d -exec chmod 755 {} +
find "$stage" -type f -exec chmod 644 {} +
chmod 755 "$stage/bin/premarket" "$stage/libexec/premarket"

if LD_LIBRARY_PATH=$stage/libs ldd "$stage/libexec/premarket" | grep 'not found' >&2; then
    rm -rf "$stage"
    die "the program needs a library this host does not have (above)"
fi
LD_LIBRARY_PATH=$stage/libs "$stage/libexec/premarket" --version >/dev/null ||
    { rm -rf "$stage"; die "the program does not run on this host (Ubuntu 22.04 or newer, x86_64)"; }

mkdir "$old"
for d in bin libexec libs share; do
    if [ -e "$prefix/$d" ]; then mv "$prefix/$d" "$old/$d"; fi
    mv "$stage/$d" "$prefix/$d"
done
for f in VERSION BUILD_SHA LICENSE THIRD_PARTY_NOTICES.txt; do mv -f "$stage/$f" "$prefix/$f"; done
rm -rf "$stage" "$old"

mkdir -p "$prefix/conf/sinks" "$prefix/conf/symbols" \
    "$prefix/state/data" "$prefix/state/contracts" "$prefix/state/logs" "$prefix/state/reports"

if [ "$layout" = 8.0 ]; then
    for v in "$prefix"/[0-9]*.[0-9]*.[0-9]*; do
        if [ -x "$v/bin/premarket" ] && [ -f "$v/VERSION" ]; then rm -rf "$v"; echo "  removed $v (the 8.0 layout)"; fi
    done
    rm -f "$prefix/current"
    echo "  removed $prefix/current: run $prefix/bin/premarket from now on"
    if grep -q "$prefix/current/share/baskets" "$prefix/conf/config.ini" 2>/dev/null; then
        cp -p "$prefix/conf/config.ini" "$prefix/conf/config.ini.8.0"
        sed -i "s|$prefix/current/share/baskets|$prefix/share/baskets|" "$prefix/conf/config.ini"
        echo "  changed $prefix/conf/config.ini: baskets_dir = $prefix/share/baskets (the old file is config.ini.8.0)"
    fi
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
    sed "s|@DIR@|$prefix|g" "$prefix/share/conf/config.ini.example" > "$prefix/conf/config.ini"
    echo "  created $prefix/conf/config.ini"
else
    echo "  kept    $prefix/conf/config.ini"
fi
seed "$prefix/share/conf/keys.ini.example" "$prefix/conf/keys.ini"
chmod 600 "$prefix/conf/keys.ini"
for f in "$prefix"/share/conf/symbols/*.txt.example; do
    seed "$f" "$prefix/conf/symbols/$(basename "$f" .example)"
done
for f in "$prefix"/share/conf/sinks/*.ini.example; do
    cp "$f" "$prefix/conf/sinks/"
done
chown -R "$owner:$group" "$prefix/conf" "$prefix/state"

"$prefix/bin/premarket" --version

cat <<EOF

Installed premarket $version in $prefix
  run      $prefix/bin/premarket (LD_LIBRARY_PATH=$prefix/libs, then $prefix/libexec/premarket)
  libs     $prefix/libs/
  config   $prefix/conf/config.ini, keys.ini (the Databento keys), sinks/<name>.ini
  state    $prefix/state/
  conf/ and state/ belong to $owner

Every command takes the config, and the US downloads the keys, by full path:
  $prefix/bin/premarket --config=$prefix/conf/config.ini --keys=$prefix/conf/keys.ini check-state

A new host runs init-state once; a host coming from 7.x runs migrate-state once instead.
EOF
