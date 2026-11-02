# syntax=docker/dockerfile:1
#
# The release image of the pipeline: Ubuntu 24.04 with Python 3.12, the libraries pinned in
# packaging/requirements.lock, and premarket compiled to a native binary with Nuitka. The
# image carries no .py or .pyc of this repository.
#
# Built from the repository root by ./dockerup.sh (as securities:latest) and by
# packaging/release.sh (as securities:<version>):
#
#   docker build -f docker/Dockerfile --build-arg BUILD_SHA=$(git rev-parse HEAD) -t securities:<version> .
#
# The build stage runs the unit tests (against requirements.lock, which adds the test tools to
# the same pins) before it compiles.
#
# Layout of /opt/securities (also the release tarball, minus the Python libraries):
#
#   bin/premarket                   launcher: sets PREMARKET_ROOT and PREMARKET_BUILD_SHA, execs the .bin
#   premarket.bin                   the compiled program
#   BUILD_SHA                       the commit it was built from, recorded in every manifest
#   conf/*.example, conf/sinks/*.example
#                                   config templates; mount the real conf/ over this
#   conf/symbols/<MIC>.txt          the download symbol lists this release was tested with
#   constituents/baskets/           basket definitions
#   requirements.lock               the libraries' pins, for running the tarball without Docker
#   LICENSE, THIRD_PARTY_NOTICES.txt

FROM ubuntu:24.04 AS base
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

FROM base AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev make python3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /src/requirements.lock
RUN pip install --break-system-packages --no-deps -r /src/requirements.lock \
    && pip check \
    && pip install --break-system-packages nuitka==4.2.2
COPY pyproject.toml requirements.txt LICENSE /src/
COPY packaging/requirements.lock /src/packaging/requirements.lock
COPY premarket /src/premarket
COPY conf /src/conf
COPY constituents /src/constituents
COPY tests /src/tests
WORKDIR /src
# The suite reads conf/config.ini and conf/sinks/*.ini; the templates stand in for them.
RUN cp conf/config.ini.example conf/config.ini \
    && cp conf/keys.ini.example conf/keys.ini \
    && for f in conf/sinks/*.ini.example; do cp "$f" "${f%.example}"; done \
    && TZ=Asia/Kolkata python3 -m pytest -q -p no:cacheprovider tests
# Accelerated mode: premarket is compiled into the binary, and everything else (the standard
# library, the pinned libraries) is imported from this image's Python.
RUN python3 -m nuitka --python-flag=-m --python-flag=no_docstrings \
        --follow-import-to=premarket --include-package=premarket \
        --file-reference-choice=runtime --remove-output \
        --output-dir=/out --output-filename=premarket.bin premarket
ARG BUILD_SHA=unknown
COPY packaging/launcher.sh packaging/notices.py packaging/requirements.lock /tmp/release/
RUN set -e; o=/opt/securities; \
    mkdir -p "$o/bin" "$o/conf/sinks" "$o/conf/symbols" "$o/constituents"; \
    install -m 0755 /out/premarket.bin "$o/"; \
    install -m 0755 /tmp/release/launcher.sh "$o/bin/premarket"; \
    echo "$BUILD_SHA" > "$o/BUILD_SHA"; \
    cp conf/*.example "$o/conf/"; \
    cp conf/sinks/*.example "$o/conf/sinks/"; \
    cp conf/symbols/*.txt "$o/conf/symbols/"; \
    cp -r constituents/baskets "$o/constituents/"; \
    cp LICENSE /tmp/release/requirements.lock "$o/"; \
    python3 /tmp/release/notices.py "$o/requirements.lock" > "$o/THIRD_PARTY_NOTICES.txt"; \
    ! find "$o" -name '*.py' -o -name '*.pyc' | grep .

FROM base AS runtime
COPY --from=build /opt/securities/requirements.lock /opt/securities/requirements.lock
# The container is the environment, so the libraries go into the system Python.
RUN pip install --break-system-packages --no-deps -r /opt/securities/requirements.lock \
    && pip check
COPY --from=build /opt/securities /opt/securities
ENV PREMARKET_ROOT=/opt/securities \
    PATH=/opt/securities/bin:$PATH \
    HOME=/tmp
WORKDIR /opt/securities
CMD ["premarket", "--help"]
