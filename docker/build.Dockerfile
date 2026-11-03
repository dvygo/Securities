# securities-build:22.04 -- the toolchain and every library premarket compiles against.
#
#   docker build -f docker/build.Dockerfile -t securities-build:22.04 docker/
#
# Ubuntu 22.04 because the prod hosts are; clang 18 + libc++ because MDF and AlphaEMS
# build that way. libc++ is linked statically, so the binary needs only C libraries at
# run time (glibc, libpq, OpenSSL, zlib, zstd) plus DuckDB's C API.

FROM ubuntu:22.04

ARG LLVM_VERSION=18
ARG CMAKE_VERSION=3.30.5
ARG DATABENTO_VERSION=0.69.0
ARG DUCKDB_VERSION=1.5.5
ARG HTTPLIB_VERSION=0.62.0
ARG JSON_VERSION=3.12.0
ARG DATE_VERSION=3.0.5
ARG DOCTEST_VERSION=2.4.12

ENV DEBIAN_FRONTEND=noninteractive \
    CC=clang-18 \
    CXX=clang++-18 \
    CXXFLAGS="-stdlib=libc++" \
    LDFLAGS="-stdlib=libc++" \
    CCACHE_DIR=/work/.cache/ccache

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg xz-utils unzip git \
    && curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key | gpg --dearmor -o /usr/share/keyrings/llvm.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/jammy/ llvm-toolchain-jammy-${LLVM_VERSION} main" \
        > /etc/apt/sources.list.d/llvm.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        clang-${LLVM_VERSION} lld-${LLVM_VERSION} \
        libc++-${LLVM_VERSION}-dev libc++abi-${LLVM_VERSION}-dev libunwind-${LLVM_VERSION}-dev \
        binutils make ninja-build ccache pkg-config python3-pip \
        zlib1g-dev libssl-dev libzstd-dev libpq-dev tzdata \
    && pip3 install --no-cache-dir cmake==${CMAKE_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# Pinned libraries, each installed with its CMake package where it has one, and each
# keeping its license under /usr/local/share/doc/<name>/ for the release notices. The
# versions are the ones databento-cpp pins for itself.
WORKDIR /tmp/src

RUN curl -fsSL -o json.tar.xz "https://github.com/nlohmann/json/releases/download/v${JSON_VERSION}/json.tar.xz" \
    && tar -xf json.tar.xz \
    && cmake -S json -B json-build -G Ninja -DJSON_BuildTests=OFF -DJSON_Install=ON \
    && cmake --install json-build \
    && install -Dm0644 json/LICENSE.MIT /usr/local/share/doc/nlohmann-json/LICENSE \
    && rm -rf /tmp/src/*

RUN curl -fsSL -o httplib.tar.gz "https://github.com/yhirose/cpp-httplib/archive/refs/tags/v${HTTPLIB_VERSION}.tar.gz" \
    && tar -xzf httplib.tar.gz \
    && cmake -S cpp-httplib-${HTTPLIB_VERSION} -B httplib-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DHTTPLIB_REQUIRE_OPENSSL=ON -DHTTPLIB_USE_BROTLI_IF_AVAILABLE=OFF -DHTTPLIB_USE_ZSTD_IF_AVAILABLE=OFF \
    && cmake --install httplib-build \
    && install -Dm0644 cpp-httplib-${HTTPLIB_VERSION}/LICENSE /usr/local/share/doc/cpp-httplib/LICENSE \
    && rm -rf /tmp/src/*

RUN curl -fsSL -o date.tar.gz "https://github.com/HowardHinnant/date/archive/refs/tags/v${DATE_VERSION}.tar.gz" \
    && tar -xzf date.tar.gz \
    && cmake -S date-${DATE_VERSION} -B date-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TZ_LIB=ON -DUSE_SYSTEM_TZ_DB=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    && cmake --build date-build && cmake --install date-build \
    && install -Dm0644 date-${DATE_VERSION}/LICENSE.txt /usr/local/share/doc/date/LICENSE \
    && rm -rf /tmp/src/*

RUN curl -fsSL --create-dirs -o /usr/local/include/doctest/doctest.h \
        "https://raw.githubusercontent.com/doctest/doctest/v${DOCTEST_VERSION}/doctest/doctest.h" \
    && curl -fsSL --create-dirs -o /usr/local/share/doc/doctest/LICENSE \
        "https://raw.githubusercontent.com/doctest/doctest/v${DOCTEST_VERSION}/LICENSE.txt"

RUN curl -fsSL -o duckdb.zip \
        "https://github.com/duckdb/duckdb/releases/download/v${DUCKDB_VERSION}/libduckdb-linux-amd64.zip" \
    && unzip -q duckdb.zip -d duckdb \
    && install -m0644 duckdb/duckdb.h /usr/local/include/ \
    && install -m0755 duckdb/libduckdb.so /usr/local/lib/ \
    && curl -fsSL --create-dirs -o /usr/local/share/doc/duckdb/LICENSE \
        "https://raw.githubusercontent.com/duckdb/duckdb/v${DUCKDB_VERSION}/LICENSE" \
    && ldconfig && rm -rf /tmp/src/*

RUN curl -fsSL -o databento.tar.gz \
        "https://github.com/databento/databento-cpp/archive/refs/tags/v${DATABENTO_VERSION}.tar.gz" \
    && tar -xzf databento.tar.gz \
    && cmake -S databento-cpp-${DATABENTO_VERSION} -B databento-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DDATABENTO_USE_EXTERNAL_JSON=ON -DDATABENTO_USE_EXTERNAL_HTTPLIB=ON -DDATABENTO_USE_EXTERNAL_DATE=ON \
        -DDATABENTO_ENABLE_UNIT_TESTING=OFF -DDATABENTO_ENABLE_EXAMPLES=OFF \
    && cmake --build databento-build && cmake --install databento-build \
    && install -Dm0644 databento-cpp-${DATABENTO_VERSION}/LICENSE /usr/local/share/doc/databento-cpp/LICENSE \
    && rm -rf /tmp/src/*

# curl --create-dirs makes 0750 directories; the build runs as the caller's uid.
RUN chmod -R a+rX /usr/local/include /usr/local/lib /usr/local/share

WORKDIR /work
CMD ["/bin/bash"]
