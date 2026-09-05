# Durable GitHub Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept GitHub pull-request deliveries durably and continue docs-impact work across City restarts.

**Architecture:** A Compose-owned Python gateway owns a SQLite inbox/outbox and both the signed HTTP endpoint and a lease-based worker loop. Existing pack lifecycle code remains unchanged; the worker invokes the existing Compose intake and reconciliation boundaries from persisted payloads.

**Tech Stack:** Python 3 standard library (`sqlite3`, `http.server`), Docker Compose, existing GitHub App/City adapters.

**Spec:** `docs/superpowers/specs/2026-09-04-github-durable-gateway-design.md`

## Global Constraints

- Do not add Redis, NATS, Postgres, or a new container image.
- Do not add gateway implementation to `gascity-packs`.
- Persist verified delivery and initial job before HTTP `202`.
- Never use `network_mode: service:city` for gateway ingress.
- Reuse existing source-key/logical-ID idempotency for GitHub and City effects.

---

### Task 1: SQLite inbox/outbox store

**Files:**
- Create: `scripts/github_durable_gateway.py`
- Test: `scripts/tests/test_github_durable_gateway.py`

**Interfaces:**
- Produces `GatewayStore.enqueue_delivery(delivery_id, event, payload, now) -> bool`.
- Produces `GatewayStore.claim(now) -> Job | None`, `complete(job_id)`, and `retry(job_id, error, now)`.
- Consumes the existing mounted `GC_SERVICE_STATE_ROOT` path.

- [ ] Write a failing test that enqueuing the same delivery UUID twice creates one `deliveries` row and one `intake` job.
- [ ] Implement schema initialization with `deliveries` and `jobs`, WAL mode, unique delivery ID, and transactionally insert the intake job.
- [ ] Write a failing test that an expired lease is reclaimable and an active lease is not.
- [ ] Implement short leases and retry state with bounded backoff.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_durable_gateway.py`.
- [ ] Commit `feat: add durable GitHub gateway queue`.

### Task 2: Signed ingress and worker loop

**Files:**
- Modify: `scripts/github_docs_impact_webhook.py`
- Modify: `scripts/github_durable_gateway.py`
- Test: `scripts/tests/test_github_durable_gateway.py`

**Interfaces:**
- Consumes `GatewayStore.enqueue_delivery` after signature/event validation.
- Worker consumes persisted `intake`, then invokes `github_docs_impact_compose_adapter.py intake --once` using a temporary payload file.
- Worker schedules `dispatch`, `harvest`, and `project` reconciliation jobs rather than depending on in-memory request handling.

- [ ] Write a failing handler test proving `202` is returned only after an accepted payload has a durable row.
- [ ] Route valid webhook payloads to `enqueue_delivery`; retain current invalid-signature and ignored-event behavior.
- [ ] Write a failing worker test proving an intake exception leaves a retryable job and does not duplicate the stored delivery.
- [ ] Implement the worker loop and use the existing adapter commands for each durable job kind.
- [ ] Run the gateway test module and existing `scripts/tests/test_github_docs_impact_compose_adapter.py`.
- [ ] Commit `feat: process GitHub intake from durable jobs`.

### Task 3: Independent Compose topology

**Files:**
- Modify: `compose.yaml`
- Modify: `nginx/nginx.conf` only if its upstream name changes
- Modify: `scripts/github-docs-impact-preflight.sh`
- Test: `scripts/tests/test_github_docs_impact_preflight.sh`

**Interfaces:**
- `github-gateway` exposes port 8080 on the ordinary Compose network.
- Nginx continues proxying `/v0/github/webhook` only to that service.
- The gateway mounts `./state/github-intake` and may read City/rig paths, but is not network-coupled to City.

- [ ] Write a failing preflight assertion that gateway does not use `network_mode: service:city`.
- [ ] Replace webhook/runtime service wiring with `github-gateway`; retain the existing City service and state mounts.
- [ ] Add health/status reporting for runnable job count and oldest runnable job.
- [ ] Run `docker compose --env-file .env config --quiet` and the preflight test.
- [ ] Commit `fix: decouple GitHub ingress from City lifecycle`.

### Task 4: Restart and dogfood acceptance

**Files:**
- Modify: `scripts/tests/test_github_docs_impact.sh`
- Modify: `README.md`
- Test: `scripts/tests/test_github_docs_impact.sh`

**Interfaces:**
- A previously accepted delivery survives City recreation.
- A docs-required PR produces a follow-up targeting its source branch.

- [ ] Add a smoke fixture that accepts a delivery, recreates City only, and asserts the queued job remains runnable.
- [ ] Add a deterministic fixture proving worker completion creates exactly one source-branch follow-up intent after restart/retry.
- [ ] Document the gateway state file and the explicit development replay command.
- [ ] Run Compose config, gateway unit tests, existing docs-impact adapter tests, and smoke script.
- [ ] Commit `test: prove GitHub gateway survives City restart`.

## Self-review

- The plan contains no new external service or pack-level gateway dependency.
- Each state-changing boundary has an idempotency key, a lease, and a replay test.
- The final task proves the exact failure mode that motivated this design.
