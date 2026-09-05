# Task 3 report: Independent Compose topology

## Status

Implemented the independent durable GitHub ingress topology while preserving
the stable Compose service key and Nginx upstream name `github-webhook`.

- `github-webhook` now runs on the ordinary Compose network, with no
  `network_mode: service:city` coupling.
- The redundant `github-docs-review-runtime` sidecar was removed; the durable
  gateway worker owns intake and reconciliation work.
- Nginx proxies the unchanged `/v0/github/webhook` route to
  `github-webhook:8080` only.
- The existing `./state/github-intake` mount remains in place. City continues
  to receive its existing read-only intake-state mount and review-state mount.
- Preflight checks the resolved Compose configuration and rejects a gateway
  that shares City's network namespace. It also reports `runnable_jobs` and
  `oldest_runnable_job` from `gateway.sqlite`.

The service key and `GC_SERVICE_NAME` intentionally remain `github-webhook`:
this preserves the public DNS/proxy contract while the implementation behind
it is the independent durable gateway.

## Tests

Passed:

```sh
PYTHONDONTWRITEBYTECODE=1 sh scripts/tests/test_github_docs_impact_preflight.sh
PYTHONDONTWRITEBYTECODE=1 sh scripts/tests/test_github_docs_impact.sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_durable_gateway.py
docker compose --env-file .env.example config --quiet
git diff --check
```

`docker compose --env-file .env config --quiet` could not run in this
worktree because `.env` is not present. The command exits with:

```text
couldn't find env file: .../.env
```

No `.env` file was created because it may contain operator-specific secrets.

## Concerns

Pre-existing untracked `scripts/__pycache__/` and
`scripts/tests/__pycache__/` directories were left untouched and are not part
of this task.
