# Contributing

Thanks for looking at Securities. This repo hosts one symbology/basket
pipeline, `v6-python/`. (The earlier `v5-python/` and `v4-golang/`
implementations were removed in 6.3.0; they remain in git history.)

## Before you start

- Open an issue first for anything beyond a small fix — new data sources,
  schema changes, or pipeline-stage restructuring should be discussed before
  you write code.
- Check existing issues/PRs so we don't duplicate work.

## Setup

```bash
cd v6-python
python -m venv .venv
.venv/bin/pip install -e .[dev]
cp conf/config.ini.example conf/config.ini
```

Run tests:

```bash
pytest
```

### Shared Postgres

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

## Making a change

1. Fork, branch off `main`.
2. Keep the diff scoped to one thing — a bug fix, one new feature, one
   refactor. Don't bundle unrelated cleanup into the same PR.
3. Match the existing style: follow `v6-python/premarketv6/`'s existing
   module shape.
4. Add/update tests for the code you touch. A behavior change with no test
   covering it will get asked for one.
5. Run the pipeline's test suite (see above) and make sure it's green.

## Secrets

Never commit real API keys, database URLs with credentials, or `.ini` files
that aren't the `.example` templates. The pipeline gitignores `config.ini`/
`keys.ini` copies — if you're not sure whether something's safe to
commit, ask in the PR rather than pushing it.

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
