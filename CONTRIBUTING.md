# Contributing

This branch is the 5.0.0 release. It holds one pipeline, `v5-python/`.

## Before you start

- For anything bigger than a small fix, open an issue first. That includes new data sources, schema changes, or reshuffling pipeline steps.
- Check existing issues and PRs so work isn't duplicated.

## Setup

```bash
cd v5-python
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp conf/config.ini.example conf/config.ini
cp conf/keys.ini.example conf/keys.ini
```

Contract DB, if you need to test the push:

```bash
docker compose -f docker/contract-postgres/docker-compose.yml up -d
```

Full operator setup is in `v5-python/docs/deploy/setup.markdown`.

## Tests

```bash
cd v5-python
pytest
```

## Making a change

1. Branch off `main`. Release fixes go on `releases/5.0.0`.
2. One thing per PR: a bug fix, a feature or a refactor. Don't mix in unrelated cleanup.
3. Follow the existing module shape in `v5-python/premarket/`.
4. Add or update tests for what you touch. A behavior change needs a test.
5. Run `pytest` before you push.
6. If you change a command, a flag or the config, update the setup doc and the runbook in `v5-python/docs/` in the same PR.

## Secrets

Never commit real API keys, database URLs with passwords, `config.ini` or `keys.ini`. Only the `.example` files belong in git. If you're not sure, ask in the PR before pushing.

## Commit messages

Use `type(scope): summary`, where type is `feat`, `fix`, `refactor`, `docs`, `test` or `chore`. Say *why* in the body when the diff doesn't make it obvious.

## Pull requests

- Say what changed and why.
- Link the issue, if there is one.

## Code of conduct

Be respectful, assume good faith, and keep feedback on the code.
