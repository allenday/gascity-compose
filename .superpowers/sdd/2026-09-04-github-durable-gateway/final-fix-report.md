# Final review fix report: durable GitHub gateway

## Status

Complete for every safe local requirement in the final review.

- Signed allowed-action deliveries now receive full installation, repository,
  base/head, PR-number, and immutable revision validation before enqueue.
  Malformed deliveries return `400 invalid_payload` and replay does not create
  SQLite state.
- Malformed deliveries already present in an older database transition to the
  terminal `failed` status and are excluded from claims and runnable counts.
- `/healthz` now reports runnable depth, the oldest runnable job, and worker
  state. It returns 503 if the worker exits, one attempt exceeds the bound, or
  consecutive retryable failures produce no successful advancement for the
  configured bounded interval (`GC_GITHUB_GATEWAY_STALL_SECONDS`, 600 seconds
  in Compose).
- Retry availability is calculated from the post-attempt failure clock, not
  the earlier claim clock.
- The gateway no longer receives City, reviewer-rig, or shared Gas City runtime
  paths or environment. Its Compose mounts are limited to the read-only GitHub
  pack and three implementation scripts plus its writable intake state.
- Restart acceptance now starts the actual Compose `github-webhook` and `city`
  service topology. It accepts a signed delivery through the live gateway,
  force-recreates City while the gateway container remains running, and then
  restarts the gateway with an expired leased job. The real gateway HTTP,
  SQLite, worker, lease, and subprocess boundaries run unchanged; deterministic
  local filesystem adapters replace only GitHub and developer-City mutations.
  Replay produces one source-keyed intake/dispatch/harvest/project effect set.
- The SDD ledger now records Task 3, Task 4, review rounds, and this final fix.

## Verification

Fresh verification returned exit status 0:

```text
PYTHONDONTWRITEBYTECODE=1 make test
  complete repository suite passed
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_durable_gateway.py
  16 tests passed
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_docs_impact_compose_adapter.py
  25 tests passed
PYTHONDONTWRITEBYTECODE=1 sh scripts/tests/test_github_docs_impact_preflight.sh
PYTHONDONTWRITEBYTECODE=1 sh scripts/tests/test_github_docs_impact.sh
  includes live Compose City recreation and leased-gateway restart; passed
docker compose --env-file .env.example --profile github-docs-impact config --quiet
sh -n scripts/tests/test_github_docs_impact.sh
sh -n scripts/tests/test_github_gateway_compose_restart.sh
git diff --check
```

No external GitHub request, Check Run, branch, pull request, or developer City
mutation was performed.

## Remaining concern

The design's real docs-required dogfood PR/check/follow-up acceptance remains
unexecuted because this fix was explicitly constrained to avoid external
GitHub side effects. The local Compose test is deliberately fail-closed: it
uses only an already-local `python:3.12-alpine` image and reports a skip when
Docker or that image is unavailable rather than pulling an image or contacting
an external service.

Pre-existing untracked `scripts/__pycache__/` and
`scripts/tests/__pycache__/` directories were left untouched.
