#!/bin/sh
# Build and test premarket inside securities-build:22.04. Docker is only the build
# environment; the binary it produces runs directly on the host.
#
#   ./build.sh           configure, build, run the tests      -> build/premarket
#   ./build.sh sanitize  the tests again under AddressSanitizer + UBSan (build-sanitize/)
#   ./build.sh image     (re)build the securities-build:22.04 image
#   ./build.sh shell     a shell in the build image, repo at /src, build dir at /build
set -eu
cd "$(dirname "$0")"
IMAGE=securities-build:22.04
mkdir -p build build-sanitize .cache/ccache

image() { docker build -f docker/build.Dockerfile -t "$IMAGE" docker/; }

run() {
    docker image inspect "$IMAGE" >/dev/null 2>&1 || image
    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e CCACHE_DIR=/src/.cache/ccache -e TZ=Asia/Kolkata \
        -v "$PWD":/src -v "$PWD/build":/build -v "$PWD/build-sanitize":/build-sanitize -w /src "$@"
}

case "${1:-build}" in
    image) image ;;
    shell) run -it "$IMAGE" bash ;;
    build)
        run "$IMAGE" sh -c 'cmake -S /src -B /build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
            -DCMAKE_CXX_COMPILER_LAUNCHER=ccache >/dev/null && ninja -C /build && /build/premarket_tests'
        echo "built: build/premarket"
        ;;
    sanitize)
        run -e ASAN_OPTIONS=detect_leaks=1:detect_stack_use_after_return=1 -e UBSAN_OPTIONS=print_stacktrace=1 \
            "$IMAGE" sh -c 'cmake -S /src -B /build-sanitize -G Ninja -DCMAKE_BUILD_TYPE=Debug \
            -DPREMARKET_SANITIZE=address,undefined -DCMAKE_CXX_COMPILER_LAUNCHER=ccache >/dev/null &&
            ninja -C /build-sanitize premarket_tests && /build-sanitize/premarket_tests'
        ;;
    *) echo "usage: ./build.sh [build|sanitize|image|shell]" >&2; exit 2 ;;
esac
