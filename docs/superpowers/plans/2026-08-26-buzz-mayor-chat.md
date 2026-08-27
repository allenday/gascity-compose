# Buzz Mayor Chat Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Tailnet-only private Buzz channel that relays generic human chat to and from the Mayor through Agent Mail.

**Architecture:** `cyberstorm-dev/gascity-gitea` provides a new Buzz-specific durable bridge and command. `allenday/gascity-compose` deploys the pinned upstream relay behind Nginx, builds that command at an immutable source revision, and owns bootstrap and live acceptance. The bridge treats content as opaque and receives no Gitea or City authority.

**Tech Stack:** Go 1.24, existing Agent Mail JSON-RPC transport conventions, Buzz JSON CLI/API, Docker Compose, Nginx, POSIX shell, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-26-buzz-mayor-chat-design.md`

## Global Constraints

- Use exactly one configured private Buzz channel and allowlisted human public keys.
- Persist logical Buzz event IDs separately from polling receipts and advance the ordered `(created_at,event_id)` cursor only after durable recording.
- Save a complete signed raw Buzz reply event before its first POST and retry the identical event ID after a timeout or restart; do not use a non-idempotent CLI send for outbound replies.
- Relay generic message content unchanged; neither repository receives a Gitea token, URL, API client, issue type, or lifecycle behavior for this feature.
- The bridge has no City API, City runtime mount, host port, or Mayor private key.
- Pin the published Buzz image and `gascity-gitea` source revision immutably; never use a floating tag or branch.
- The delivery state machine and the parent acceptance issue require independent critique, current CI, and live acceptance evidence before explicit closure.

---

## File and dependency map

| Task | Repository | Files | Depends on | Produces |
| --- | --- | --- | --- | --- |
| 1. Bridge core | `cyberstorm-dev/gascity-gitea` | new `buzzbridge/`, `cmd/buzz-mayor-bridge/` | none | durable bidirectional adapter and tests |
| 1a. Public-host core amendment | `cyberstorm-dev/gascity-gitea` | `buzzbridge/`, command tests | 1 | canonical public Host authority over private relay transport |
| 2. Relay foundation | `allenday/gascity-compose` | `compose.yaml`, `nginx/nginx.conf`, `.env.example`, test | none | pinned Tailnet-only Buzz relay profile |
| 3. Bridge deployment | `allenday/gascity-compose` | `compose.yaml`, `Makefile`, bootstrap/up scripts, test, CI | 1, 1a, 2 | isolated deployed bridge profile |
| 4. Live fixture | `allenday/gascity-compose` | smoke script, Makefile, README, test | 3 | repeatable end-to-end evidence |
| 5. Parent integration | both repositories | issue/PR evidence only | 1–4 | independently reviewed assembled delivery |

Tasks 1 and 2 are independent. Task 1a is a focused core amendment required
to preserve Buzz's canonical public community Host over private transport.
Task 3 is their fan-in and must obtain an interface-completeness review before
its PR. Tasks 4 and 5 are sequential acceptance work.

### Task 1: Buzz Mayor bridge core

**Repository:** `cyberstorm-dev/gascity-gitea`

**Files:**
- Create: `buzzbridge/types.go`, `buzzbridge/ledger.go`, `buzzbridge/controller.go`, `buzzbridge/buzz.go`, `buzzbridge/mail.go`
- Create: `buzzbridge/ledger_test.go`, `buzzbridge/controller_test.go`, `buzzbridge/buzz_test.go`
- Create: `cmd/buzz-mayor-bridge/main.go`, `cmd/buzz-mayor-bridge/main_test.go`
- Modify: `go.mod` only if an existing standard-library/client dependency cannot provide the required bounded Buzz command execution.

**Interfaces:**
- Consumes: `BuzzClient.Messages(ctx, channel string, cursor Cursor, limit int) ([]BuzzEvent, error)` and `BuzzClient.Publish(ctx, event SignedEvent) error`.
- Consumes: `MailClient.Receive(ctx, identity string, cursor string, limit int) ([]MailMessage, error)`, `MailClient.Send(ctx, envelope MayorEnvelope) (string, error)`, and `MailClient.Acknowledge(ctx, identity, messageID string) error`.
- Produces: `Controller.Reconcile(ctx context.Context) error`; it has no tracker or City interfaces.
- Produces: JSON ledger records `InboundRecord{BuzzEventID, ChannelID, ThreadRootID, MailMessageID}` and `OutboundRecord{MailMessageID, SignedEvent, BuzzEventID}` plus `Cursor{CreatedAt,EventID}`.

- [ ] **Step 1: Write failing ledger and controller tests.**

```go
func TestReconcileDuplicateBuzzEventDeliversMayorMailOnce(t *testing.T) {
    controller := newTestController(t, []BuzzEvent{{ID: "evt-1", Channel: "mayor", Author: "human-key", Content: "hello"}})
    require.NoError(t, controller.Reconcile(context.Background()))
    require.NoError(t, controller.Reconcile(context.Background()))
    require.Len(t, controller.mail.sent, 1)
}

func TestReconcileRestartsWithoutDuplicatingOutboundReply(t *testing.T) {
    controller := newTestControllerWithMappedInbound(t)
    require.NoError(t, controller.Reconcile(context.Background()))
    controller = reopenTestController(t, controller.ledgerPath)
    require.NoError(t, controller.Reconcile(context.Background()))
    require.Len(t, controller.buzz.sent, 1)
}

func TestReconcileSameTimestampPageResumesByEventID(t *testing.T) {
    controller := newSaturatedTimestampController(t, "evt-2")
    require.NoError(t, controller.Reconcile(context.Background()))
    require.Equal(t, Cursor{CreatedAt: 1700000000, EventID: "evt-3"}, controller.Cursor())
}
```

- [ ] **Step 2: Run the focused Go test package and confirm the missing package/API fails.**

Run: `go test ./buzzbridge ./cmd/buzz-mayor-bridge`

Expected: FAIL because `buzzbridge` and command configuration do not yet exist.

- [ ] **Step 3: Implement the smallest durable adapter.**

Implement a file-atomic JSON ledger with separate inbound/outbound records and a persisted bounded `(created_at,event_id)` polling cursor. Filter before delivery on the fixed channel and exact allowlisted signer. Write the inbound record before Agent Mail send. For outbound mail, construct a deterministic signed raw Buzz event with the logical mail-receipt ID in a dedicated tag, persist its complete bytes before the first POST, and retry that identical event after an uncertain outcome. Complete each record only after the corresponding side effect succeeds. Use a typed `gc.buzz.mayor.message.v1` envelope that contains a mapping key and opaque content. Reject unmapped, malformed, wrong-channel, and wrong-signer inputs without acknowledgement.

- [ ] **Step 4: Add command configuration and health behavior.**

`configFromEnv` must require `BUZZ_RELAY_URL`, `BUZZ_MAYOR_CHANNEL_ID`, `BUZZ_ALLOWED_HUMAN_PUBKEYS`, `BUZZ_BRIDGE_PRIVATE_KEY`, `BUZZ_LEDGER_PATH`, Agent Mail endpoint/identity/destination, positive poll interval, and positive bounded batch size. It must reject blank or duplicate allowlist values. Expose `/healthz` and `/readyz`; readiness becomes healthy only after one successful reconciliation and becomes unhealthy after the configured stale interval.

- [ ] **Step 5: Run tests and static checks.**

Run: `go test ./...`

Expected: PASS. Confirm package imports contain no `giteaissue`, `intake`, or City client dependency.

- [ ] **Step 6: Commit.**

```bash
git add buzzbridge cmd/buzz-mayor-bridge go.mod go.sum
git commit -m "feat: add durable Buzz Mayor mail bridge"
```

### Task 1a: Canonical public relay-host core amendment

**Repository:** `cyberstorm-dev/gascity-gitea`

**Files:**
- Modify: `buzzbridge/buzz.go`, `buzzbridge/buzz_test.go`, `cmd/buzz-mayor-bridge/main.go`, `cmd/buzz-mayor-bridge/main_test.go`

**Interfaces:**
- Consumes: private `BUZZ_RELAY_URL` transport endpoint and canonical external `BUZZ_PUBLIC_RELAY_URL`.
- Produces: every raw query/event HTTP request targets the private endpoint while carrying the canonical public URL host authority; no caller controls that header.

- [ ] **Step 1: Write failing transport-host tests.**

Assert an HTTP test server receives its private destination while the request
Host equals the canonical public authority. Assert missing/malformed public
coordinate fails command configuration and an inbound message cannot affect the
Host header.

- [ ] **Step 2: Implement strict dual-coordinate configuration.**

Parse both absolute HTTP(S) URLs, reject credentials/query/fragments, retain
the public host authority separately, and set it only in the Buzz client
transport. Keep NIP-98 signing bound to the actual request URL; do not use
arbitrary user-supplied Host data.

- [ ] **Step 3: Add authenticated preflight and verify.**

Expose a bounded authenticated query operation usable by Compose preflight.
Run `go test ./... -race -count=1` and `go vet ./...`, then commit:

```bash
git add buzzbridge cmd/buzz-mayor-bridge
git commit -m "fix: preserve Buzz public relay host authority"
```

### Task 2: Pinned Tailnet-only Buzz relay foundation

**Repository:** `allenday/gascity-compose`

**Files:**
- Modify: `compose.yaml`, `nginx/nginx.conf`, `.env.example`, `.github/workflows/ci.yml`, `Makefile`
- Create: `scripts/tests/test_buzz_profile.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: a resolved immutable upstream image reference `ghcr.io/block/buzz:sha-<short-sha>` verified against upstream publication.
- Produces: profile `buzz`, service `buzz-relay`, Nginx `BUZZ_PORT`, health endpoints `/_liveness` and `/_readiness`.

- [ ] **Step 1: Write the Compose contract test first.**

The shell test renders `docker compose --env-file .env.example --profile buzz config`, extracts service blocks, and fails unless the relay has no `ports:`, durable mounts exist for relay/Postgres/Redis/object storage/Git state, the image is immutable, and Nginx has both the configurable Tailnet port and a `buzz-relay` backend with WebSocket forwarding.

- [ ] **Step 2: Run the test and confirm it fails before configuration exists.**

Run: `sh scripts/tests/test_buzz_profile.sh`

Expected: FAIL because profile `buzz` and `BUZZ_PORT` are absent.

- [ ] **Step 3: Add the self-hosted relay profile.**

Port the necessary upstream single-node services into `profiles: [buzz]`, bind all mutable state under `./state/buzz-*`, use non-secret defaults only where upstream permits, and source private relay/storage secrets from ignored `.env`. Add `BUZZ_PORT`, canonical external URL documentation, and explicit secret placeholders to `.env.example`. Publish only `${TAILNET_IP}:${BUZZ_PORT}` on Nginx; keep relay itself internal.

- [ ] **Step 4: Add Make and CI validation.**

Add `buzz-up` that runs the profile with `--wait`; include `buzz` in profile rendering validation and run the new shell contract test from `make test` and GitHub Actions.

- [ ] **Step 5: Run verification.**

Run: `docker compose --env-file .env.example --profile buzz config --quiet && sh scripts/tests/test_buzz_profile.sh && make test`

Expected: PASS without starting a container.

- [ ] **Step 6: Commit.**

```bash
git add compose.yaml nginx/nginx.conf .env.example Makefile README.md .github/workflows/ci.yml scripts/tests/test_buzz_profile.sh
git commit -m "feat: add pinned private Buzz relay profile"
```

### Task 3: Deploy the isolated Buzz Mayor bridge

**Repository:** `allenday/gascity-compose`

**Files:**
- Modify: `compose.yaml`, `.env.example`, `Makefile`, `.github/workflows/ci.yml`, `README.md`
- Create: `scripts/buzz-mayor-bridge-bootstrap.sh`, `scripts/buzz-mayor-bridge-preflight.sh`, `scripts/tests/test_buzz_mayor_bridge.sh`

**Interfaces:**
- Consumes: Task 1a command `./cmd/buzz-mayor-bridge` at an immutable `GASCITY_GITEA_REF`, `BUZZ_PUBLIC_RELAY_URL`, and private `BUZZ_RELAY_URL`, plus Task 2 healthy `buzz-relay`.
- Produces: profile `buzz-mayor-bridge` and commands `make buzz-mayor-bridge-bootstrap`, `make buzz-mayor-bridge-up`, and `make buzz-mayor-bridge-smoke`.

- [ ] **Step 1: Obtain an independent interface-completeness review before writing the PR.**

Review the actual Task 1 commit and resolved Buzz image/CLI contract. Record that the source pin is immutable; relay/channel/signer are validated; logical event identity differs from polling receipts; cursor/outbox recovery is durable; bootstrap owns private membership; the service has no Gitea/City authority; and the fixture can prove both directions.

- [ ] **Step 2: Write the failing deployment contract test.**

Assert the bridge has `profiles: [buzz-mayor-bridge]`, no `ports`, a sole bridge ledger mount, dependencies on healthy `buzz-relay` and `mcp-agent-mail`, and no `GITEA_`, `GASCITY_`, City mount, or Mayor private-key environment. Assert the build checks a clean pinned `gascity-gitea` checkout and builds only `./cmd/buzz-mayor-bridge`. Assert private transport and canonical public Host are distinct explicit configuration values and a local `/readyz` probe is the Compose health check.

- [ ] **Step 3: Run the test and confirm it fails.**

Run: `sh scripts/tests/test_buzz_mayor_bridge.sh`

Expected: FAIL because the bridge profile and bootstrap are absent.

- [ ] **Step 4: Implement deployment, bootstrap, and preflight.**

Add the non-root bridge service and `mcp-agent-mail` profile membership. Map a private internal relay destination into `BUZZ_RELAY_URL` and canonical Tailnet community URL into `BUZZ_PUBLIC_RELAY_URL`; the core sets only the latter as HTTP Host authority. Bootstrap uses only Buzz administration/CLI credentials to create-or-verify the configured private channel and memberships, generates a bridge key once, stores it in ignored state/config, and writes stable public/channel coordinates. Preflight rejects unset coordinates, malformed or duplicate human public keys, a failed authenticated internal `/query` with the canonical Host, floating provider pins, or missing Agent Mail identity. It must not create a Gitea account, token, label, webhook, repository, or City work.

- [ ] **Step 5: Wire command targets and CI.**

`buzz-mayor-bridge-up` runs preflight then starts `buzz`, `mcp-agent-mail`, and `buzz-mayor-bridge` with `--wait`. `make test` and CI run both Buzz contract tests and profile rendering. Document the one-time bootstrap and least-authority boundary.

- [ ] **Step 6: Run verification and commit.**

Run: `docker compose --env-file .env.example --profile buzz --profile buzz-mayor-bridge config --quiet && sh scripts/tests/test_buzz_profile.sh && sh scripts/tests/test_buzz_mayor_bridge.sh && make test`

Expected: PASS.

```bash
git add compose.yaml .env.example Makefile README.md .github/workflows/ci.yml scripts/buzz-mayor-bridge-bootstrap.sh scripts/buzz-mayor-bridge-preflight.sh scripts/tests/test_buzz_mayor_bridge.sh
git commit -m "feat: deploy isolated Buzz Mayor bridge"
```

### Task 4: Repeatable live acceptance fixture

**Repository:** `allenday/gascity-compose`

**Files:**
- Create: `scripts/buzz-mayor-bridge-smoke.sh`
- Modify: `Makefile`, `README.md`, `scripts/tests/test_buzz_mayor_bridge.sh`

**Interfaces:**
- Consumes: the bootstrap-created channel, allowlisted fixture identity, bridge ledger, and Mayor Agent Mail destination.
- Produces: durable acceptance output identifying channel, source event, mapped mail message, response event, and revision pins without logging secrets.

- [ ] **Step 1: Write a failing static test for the smoke contract.**

Require the smoke script to use the configured private channel, send through an allowlisted fixture identity, wait with bounded retries for the bridge mapping, inject a controlled Mayor reply through Agent Mail, assert `reply-to` is the original thread root, replay the inbound event, restart the bridge, and assert one logical inbound/outbound mapping remains.

- [ ] **Step 2: Implement the smoke script.**

Use Buzz's JSON CLI with explicit environment variables. The script must redact keys, create no arbitrary room, preserve opaque content, and print immutable source/image revisions plus non-secret event IDs. It exits non-zero on duplicate, missing mapping, bad thread, failed readiness, or unallowlisted sender acceptance.

- [ ] **Step 3: Verify and commit.**

Run: `sh scripts/tests/test_buzz_mayor_bridge.sh && make buzz-mayor-bridge-smoke ENV_FILE=.env`

Expected: static test PASS; live smoke PASS only in an authorized deployed environment.

```bash
git add scripts/buzz-mayor-bridge-smoke.sh scripts/tests/test_buzz_mayor_bridge.sh Makefile README.md
git commit -m "test: add Buzz Mayor bridge acceptance smoke"
```

### Task 5: Parent integration and formal closure

**Repository:** issue tracker only; no code change.

**Depends on:** Tasks 1–4 are explicitly closed after their own current critique, CI, and acceptance gates.

- [ ] **Step 1: Independently review the integrated delivered revisions.**

Verify the source pin used by Compose is the reviewed Task 1 merge, the upstream Buzz pin is current and immutable, profile behavior matches the spec, and no later merge invalidated a child gate.

- [ ] **Step 2: Run parent CI and live acceptance.**

Record the exact CI run URLs/immutable revisions and the redacted smoke evidence. Confirm no Gitea integration behavior or City authority was introduced.

- [ ] **Step 3: Advance the parent through IDD and close explicitly.**

Append current `critique → ci → acceptance → ready_to_close` events, re-read every child and parent state, explicitly close the parent, then confirm native closure and terminal event preservation.
