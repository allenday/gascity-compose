# Task 4 report: Restart and dogfood acceptance

## Status

Implemented deterministic restart acceptance coverage without changing the
GitHub pack or creating an external GitHub pull request.

- `scripts/tests/test_github_docs_impact.sh` now accepts a delivery into the
  real SQLite gateway store, recreates only a disposable City fixture, and
  proves the queued intake job remains runnable from the mounted state.
- The same smoke test drives the real gateway lifecycle boundaries through a
  retry after a simulated City recreation. Its controller boundary durably
  records one source-keyed follow-up intent with base
  `feature/docs`; the retry adopts that record and completes the project job
  without adding another intent.
- `README.md` identifies `state/github-intake/gateway.sqlite` as the gateway
  state file and gives the explicit, bounded external controller replay
  command for development.

## Tests

Passed:

```sh
docker compose --env-file .env.example --profile github-docs-impact config --quiet
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_durable_gateway.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_docs_impact_compose_adapter.py
PYTHONDONTWRITEBYTECODE=1 sh scripts/tests/test_github_docs_impact.sh
sh -n scripts/tests/test_github_docs_impact.sh
```

## Concerns

The acceptance fixture is intentionally deterministic and local: it uses the
existing controller adapter boundary and makes no real GitHub request or pull
request. The external GitHub pack/controller remains unmodified and is still
the development replay target.

Pre-existing untracked `scripts/__pycache__/` and
`scripts/tests/__pycache__/` directories were left untouched.
