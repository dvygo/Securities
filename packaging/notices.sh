#!/bin/sh
# THIRD_PARTY_NOTICES.txt for what the release ships: the libraries linked into
# libexec/premarket, and every library in libs/. Runs inside securities-build:22.04, where
# every pinned library keeps its license under /usr/local/share/doc/<name>/ and every
# Ubuntu one under /usr/share/doc/<package>/copyright. A file in libs/ that no package
# owns stops the build. glibc is the host's own and not shipped.
set -eu
out=$1
libs=$2
section() {
    printf '%s\n\n' "================================================================================"
    printf '%s\n' "$1" "$2"
    printf '\n'
    cat "$3"
    printf '\n\n'
}
{
    printf 'premarket ships with the following third-party software.\n\n'
    section "databento-cpp $DATABENTO_VERSION (and libdbn_c, the DBN library it links)" "https://github.com/databento/databento-cpp -- Apache License 2.0" /usr/local/share/doc/databento-cpp/LICENSE
    section "DuckDB $DUCKDB_VERSION (libs/libduckdb.so)" "https://github.com/duckdb/duckdb -- MIT License" /usr/local/share/doc/duckdb/LICENSE
    section "PostgreSQL $POSTGRES_VERSION client library (libpq, statically linked)" "https://www.postgresql.org -- PostgreSQL License" /usr/local/share/doc/postgresql/LICENSE
    section "nlohmann/json $JSON_VERSION" "https://github.com/nlohmann/json -- MIT License" /usr/local/share/doc/nlohmann-json/LICENSE
    section "cpp-httplib $HTTPLIB_VERSION" "https://github.com/yhirose/cpp-httplib -- MIT License" /usr/local/share/doc/cpp-httplib/LICENSE
    section "date (Howard Hinnant) $DATE_VERSION" "https://github.com/HowardHinnant/date -- MIT License" /usr/local/share/doc/date/LICENSE
    section "LLVM libc++ / libc++abi 18 (statically linked)" "https://libcxx.llvm.org -- Apache License 2.0 with LLVM Exceptions" /usr/share/doc/libc++-18-dev/copyright
    owners=
    for so in $(ls "$libs"); do
        [ "$so" != libduckdb.so ] || continue
        pkg=$(dpkg -S "*/x86_64-linux-gnu/$so" 2>/dev/null | sed -n "s|^\([^:]*\):[^ ]*: .*/$so\$|\1|p" | head -n 1)
        [ -n "$pkg" ] || { echo "notices.sh: no Ubuntu package owns libs/$so" >&2; exit 1; }
        owners="$owners$pkg $so
"
    done
    printf '%s' "$owners" | cut -d' ' -f1 | sort -u | while read -r pkg; do
        files=$(printf '%s' "$owners" | awk -v p="$pkg" '$1 == p { printf "%slibs/%s", sep, $2; sep = ", " }')
        section "$pkg $(dpkg-query -W -f='${Version}' "$pkg") ($files)" "Ubuntu 22.04 package $pkg" "/usr/share/doc/$pkg/copyright"
    done
} > "$out"
