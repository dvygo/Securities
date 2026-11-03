# Contributing

Thanks for looking at Securities. This repo hosts one symbology/basket
pipeline, `premarket`: a C++20 program built with clang 18 and libc++ inside
`securities-build:22.04`. (Before 8.0.0 it was the Python package `premarket`,
and before 7.0.0 `premarketv6` under `v6-python/`. All of them remain in git
history.)

## Before you start

- Open an issue first for anything beyond a small fix — new data sources,
  schema changes, or pipeline-stage restructuring should be discussed before
  you write code.
- Check existing issues/PRs so we don't duplicate work.

## Setup

You need Docker; everything else is in the build image.

```bash
./build.sh              # builds the image the first time, then the binary and the tests, and runs them
./build.sh sanitize     # the tests under AddressSanitizer + UBSan
./build.sh shell        # a shell in the image: cmake -S /src -B /build -G Ninja, ninja -C /build
```

The binary is `build/premarket`. It looks for `libduckdb.so` in `../lib` beside
itself, as a release lays it out, and otherwise finds it only in the image: run
it through `./build.sh shell`, or build a release with `packaging/release.sh`
and run that on the host.

### Shared Postgres

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

## Making a change

1. Fork, branch off the release branch you are changing (`releases/8.0.0`).
2. Keep the diff scoped to one thing — a bug fix, one new feature, one
   refactor. Don't bundle unrelated cleanup into the same PR.
3. Match the existing style: one header in `include/premarket/` and one source
   in `src/` per module, the same names, `-Wall -Wextra -Wpedantic -Wshadow
   -Werror` clean.
4. **Comments live in the tests, not in the code.** `.cpp` and `.hpp` files under
   `include/` and `src/` carry almost no comments, so the code reads straight
   through when you debug it. Why a module behaves as it does — the rule, the
   incident that caused it, the edge case — goes in its
   `tests/test_<module>.hpp` (the module's description) and
   `tests/test_<module>.cpp` (next to the case that pins it). A new module gets
   both files.
5. Add or update tests for the code you touch. A behavior change with no test
   covering it will get asked for one. Where 7.0.0 defined the behavior, the
   expected values come from what 7.0.0 produced (`tests/data/`), not from
   reading the new code.
6. `./build.sh` and `./build.sh sanitize` both green.

## Secrets

Never commit real API keys, database URLs with credentials, or `.ini` files
that aren't the `.example` templates. `config.ini`, `keys.ini` and
`conf/sinks/*.ini` are gitignored — if you're not sure whether something's safe
to commit, ask in the PR rather than pushing it.

## Commit messages

Conventional Commits style: `type(scope): summary` — `feat`, `fix`, `refactor`,
`docs`, `test`, `chore`. Explain *why* in the body if the diff alone doesn't
make it obvious; skip the body if it's self-explanatory.

## Pull requests

- Describe what changed and why, not just what the diff shows.
- Link the issue it addresses, if any.

## Code of conduct

Be respectful, assume good faith, keep feedback focused on the code. Nothing
formal beyond that for now.
