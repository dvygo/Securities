# syntax=docker/dockerfile:1
#
# The release image of the v5 pipeline: Ubuntu 24.04 with Python 3.12, the pinned libraries
# of packaging/requirements.lock, and premarket and strategies compiled to native binaries
# with Nuitka. The image carries no .py or .pyc of this repository.
#
# Built by packaging/release.sh, from the repository root:
#
#   docker build -f packaging/Dockerfile -t securities:<version> .
#
# The build stage runs the unit tests against the pinned libraries before it compiles.
#
# Layout of /opt/securities (also the release tarball, minus the Python libraries):
#
#   bin/premarket, bin/strategies   launchers: set PREMARKET_V5_ROOT, exec the .bin
#   premarket.bin, strategies.bin   the compiled programs
#   conf/*.example                  config templates; mount the real conf/ over this
#   constituents/baskets/           basket templates
#   strategies/<NAME>/underlying.txt
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
COPY packaging/requirements.lock /opt/securities/requirements.lock
# The container is the environment, so the libraries go into the system Python.
RUN pip install --break-system-packages --no-deps -r /opt/securities/requirements.lock \
    && pip check

FROM base AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev make python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages nuitka==4.2.2 pytest==9.1.1
COPY v5-python /src
WORKDIR /src
RUN python3 -m pytest -q -p no:cacheprovider tests
# Accelerated mode: premarket and strategies are compiled into each binary, and everything
# else (the standard library, the pinned libraries) is imported from this image's Python.
# --file-reference-choice=runtime makes __file__ relative to the binary, so a strategy finds
# its underlying.txt at /opt/securities/strategies/<NAME>/ next to strategies.bin.
RUN for prog in premarket strategies; do \
        python3 -m nuitka --python-flag=-m --python-flag=no_docstrings \
            --follow-import-to=premarket --follow-import-to=strategies \
            --include-package=premarket --include-package=strategies \
            --file-reference-choice=runtime --remove-output \
            --output-dir=/out --output-filename="$prog.bin" "$prog" || exit 1; \
    done
COPY packaging/launcher.sh packaging/notices.py LICENSE /tmp/release/
RUN set -e; o=/opt/securities; \
    mkdir -p "$o/bin" "$o/conf" "$o/constituents"; \
    install -m 0755 /out/premarket.bin /out/strategies.bin "$o/"; \
    for prog in premarket strategies; do install -m 0755 /tmp/release/launcher.sh "$o/bin/$prog"; done; \
    cp conf/*.example "$o/conf/"; \
    cp -r constituents/baskets "$o/constituents/"; \
    for d in strategies/*/; do \
        n=$(basename "$d"); [ -f "$d/underlying.txt" ] || continue; \
        mkdir -p "$o/strategies/$n"; cp "$d/underlying.txt" "$o/strategies/$n/"; \
    done; \
    cp /tmp/release/LICENSE "$o/"; \
    python3 /tmp/release/notices.py "$o/requirements.lock" > "$o/THIRD_PARTY_NOTICES.txt"; \
    ! find "$o" -name '*.py' -o -name '*.pyc' | grep .

FROM base AS runtime
COPY --from=build /opt/securities /opt/securities
ENV PREMARKET_V5_ROOT=/opt/securities \
    PATH=/opt/securities/bin:$PATH \
    HOME=/tmp
WORKDIR /opt/securities
CMD ["premarket", "--help"]
