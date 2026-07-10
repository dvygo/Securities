# Contributing

Thanks for looking at Securities. This repo hosts two parallel
implementations of the same symbology/basket pipeline — `v5-python/`
(active) and `v4-golang/` (maintained). Pick whichever matches the change
you're making; don't port a fix across both in the same PR unless it's the
same root cause in both, and say so explicitly if you do.

## Before you start

- Open an issue first for anything beyond a small fix — new data sources,
  schema changes, or pipeline-stage restructuring should be discussed before
  you write code.
- Check existing issues/PRs so we don't duplicate work.

## Setup

### v5-python

```bash
cd v5-python
python -m venv .venv
.venv/Scripts/pip install -e .[dev]   # Linux/macOS: source .venv/bin/activate first
cp conf/config.ini.example conf/config.ini
```

Run tests:

```bash
pytest
```

### v4-golang

```powershell
cd v4-golang
copy conf\config.example.ini conf\config.ini
go build ./...
go test ./...
```

### Shared Postgres

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

## Making a change

1. Fork, branch off `main`.
2. Keep the diff scoped to one thing — a bug fix, one new feature, one
   refactor. Don't bundle unrelated cleanup into the same PR.
3. Match the existing style in whichever pipeline you're touching (Python:
   follow `v5-python/premarket/`'s existing module shape; Go: follow
   `v4-golang/internal/`'s package layout).
4. Add/update tests for the code you touch. A behavior change with no test
   covering it will get asked for one.
5. Run the pipeline's test suite (see above) and make sure it's green.

## Secrets

Never commit real API keys, database URLs with credentials, or `.ini` files
that aren't the `.example` templates. Both pipelines gitignore `config.ini`/
`config.example.ini` copies — if you're not sure whether something's safe to
commit, ask in the PR rather than pushing it.

## Commit messages

Conventional Commits style: `type(scope): summary` — `feat`, `fix`, `refactor`,
`docs`, `test`, `chore`. Explain *why* in the body if the diff alone doesn't
make it obvious; skip the body if it's self-explanatory.

## Pull requests

- Describe what changed and why, not just what the diff shows.
- Link the issue it addresses, if any.
- Note if it touches `v5-python`, `v4-golang`, or both — and if both, confirm
  they were tested independently.

## Code of conduct

Be respectful, assume good faith, keep feedback focused on the code. Nothing
formal beyond that for now.
