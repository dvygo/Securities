#!/bin/bash
# The release smoke test, run by release.sh as root on a bare ubuntu:22.04 with the tarball in
# /dist and the test fixture in /fixture. The host gets nothing but tzdata and sudo, and an
# account `ops` that installs through sudo and runs premarket.
#
# install.sh must refuse without sudo, without --prefix, into a directory that holds something
# else, and while a run holds the numbering lock. Installed, the program files belong to root
# (bin/premarket and libexec/premarket 0755, libs/ 0644), conf/ and state/ to ops, keys.ini is
# 0600, libexec/premarket finds no library on its own (no RUNPATH) and every library outside
# glibc from DIR/libs through LD_LIBRARY_PATH. ops then numbers a day of the fixture end to
# end, reinstalls over it with conf/ and the state untouched, and migrates the 7.x fixture.
set -euo pipefail
name=$1
DIR=/srv/securities-smoke
apt-get update -qq >/dev/null && apt-get install -y -qq tzdata sudo >/dev/null 2>&1
useradd -m ops
echo 'ops ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ops
cd /tmp && tar -xzf "/dist/$name.tar.gz"
I=/tmp/$name/install.sh
as_ops() { su ops -s /bin/bash -c "$1"; }
refused() {
    if as_ops "$1" > /tmp/out 2>&1; then echo "smoke: not refused: $1" >&2; exit 1; fi
    grep -q -- "$2" /tmp/out || { echo "smoke: refused without \"$2\": $1" >&2; cat /tmp/out >&2; exit 1; }
    echo "refused as it should: $1"
}
fail() { echo "smoke: $*" >&2; exit 1; }

refused "$I --prefix $DIR" "must be run with sudo"
if "$I" --prefix "$DIR" > /tmp/out 2>&1; then fail "root without sudo was not refused"; fi
grep -q "must be run with sudo" /tmp/out || fail "root without sudo: $(cat /tmp/out)"
echo "refused as it should: root without sudo"
refused "sudo $I" "--prefix DIR is required"
refused "sudo $I --prefix relative/dir" "absolute path"
refused "sudo $I --prefix /usr" "holds no premarket install"
[ ! -e "$DIR" ] || fail "a refused install left $DIR behind"

as_ops "sudo $I --prefix $DIR" > /tmp/install.out
tail -n 12 /tmp/install.out

mode() { stat -c '%a %U' "$1"; }
[ "$(mode $DIR/bin/premarket)" = "755 root" ] || fail "bin/premarket is $(mode $DIR/bin/premarket)"
[ "$(mode $DIR/libexec/premarket)" = "755 root" ] || fail "libexec/premarket is $(mode $DIR/libexec/premarket)"
for f in "$DIR"/libs/*; do [ "$(mode "$f")" = "644 root" ] || fail "$f is $(mode "$f")"; done
[ "$(stat -c %U $DIR/conf)" = ops ] && [ "$(stat -c %U $DIR/state/data)" = ops ] || fail "conf/ and state/ are not ops's"
[ "$(mode $DIR/conf/keys.ini)" = "600 ops" ] || fail "keys.ini is $(mode $DIR/conf/keys.ini)"
grep -q "^baskets_dir *= $DIR/share/baskets" $DIR/conf/config.ini || fail "config.ini does not point at $DIR"
grep -q "^LD_LIBRARY_PATH=$DIR/libs$" $DIR/bin/premarket || fail "bin/premarket does not set LD_LIBRARY_PATH"

ldd $DIR/libexec/premarket | grep -q 'libduckdb.so => not found' || fail "libexec/premarket finds libduckdb.so without LD_LIBRARY_PATH"
LD_LIBRARY_PATH=$DIR/libs ldd $DIR/libexec/premarket > /tmp/ldd
if grep 'not found' /tmp/ldd; then fail "a library is missing"; fi
outside=$(grep '=> /' /tmp/ldd | grep -v "=> $DIR/libs/" | grep -v -E '/(libc|libm|libdl|libpthread)\.so|ld-linux' || true)
[ -z "$outside" ] || fail "loaded from outside $DIR/libs: $outside"
echo "libs/: $(ls $DIR/libs | tr '\n' ' ')"

PM="$DIR/bin/premarket --config=$DIR/conf/config.ini --keys=$DIR/conf/keys.ini"
as_ops "$PM --version"
D=$DIR/state/data
as_ops "mkdir -p $D/20261103 && cp -r /fixture/state7x/20261103/X* $D/20261103/"
as_ops "$PM init-state --reason 'release smoke test'" > /dev/null
for v in XNSE XBOM XIMC; do as_ops "$PM normalize --venue $v --date-dir 20261103 --reason 'release smoke test'" > /dev/null; done
for c in check-state 'check-tokens --venue XNSE --dates 20261103' 'check-lineage --venue XNSE --dates 20261103'; do
    out=$(as_ops "$PM $c" | grep 'check(s):'); echo "$c: $out"
done

as_ops "flock $D/_state/.lock sleep 4" &
holder=$!
sleep 1
refused "sudo $I --prefix $DIR" "holds $D/_state/.lock"
wait $holder

as_ops "echo '# edited on this host' >> $DIR/conf/keys.ini"
before=$(md5sum $DIR/conf/config.ini $DIR/conf/keys.ini)
as_ops "sudo $I --prefix $DIR" > /tmp/install.out
[ "$(md5sum $DIR/conf/config.ini $DIR/conf/keys.ini)" = "$before" ] || fail "a reinstall changed conf/"
grep -q "kept    $DIR/conf/keys.ini" /tmp/install.out || fail "a reinstall did not keep keys.ini"
[ "$(stat -c %U $DIR/conf)" = ops ] || fail "a reinstall changed conf/'s owner"
out=$(as_ops "$PM check-state" | grep 'check(s):'); echo "after a reinstall, check-state: $out"

as_ops "mkdir -p /tmp/m && cp -r /fixture/state7x /tmp/m/data && sed 's|^data_dir .*|data_dir = /tmp/m/data|' $DIR/conf/config.ini > /tmp/m/config.ini"
M="$DIR/bin/premarket --config=/tmp/m/config.ini"
as_ops "$M migrate-state --reason 'release smoke test'" | tail -n 1
for v in XNSE XBOM XIMC; do as_ops "$M normalize --venue $v --date-dir 20261103 --reason 'release smoke test'" > /dev/null; done
out=$(as_ops "$M check-state" | grep 'check(s):'); echo "after migrate-state, check-state: $out"
echo "smoke test passed"
