# City Mail Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a private City launcher that turns one authenticated intake authorization into one `superpowers-build` City workflow and a durable bridge binding.

**Architecture:** `city-mail-launcher` owns only its Agent Mail identity and a durable authorization ledger. It validates one authorization, runs the fixed Mayor formula through `gc sling`, returns a binding, then acknowledges the authorization. It has no Gitea or City HTTP credential.

**Tech Stack:** POSIX shell, Python 3 standard library, Docker Compose, Gas City CLI, Agent Mail HTTP MCP, jq.

**Spec:** `docs/superpowers/specs/2026-08-26-city-mail-launcher-design.md`

## Global Constraints

- No host port, Gitea credential, webhook secret, Mayor Mail credential, `GASCITY_API_URL`, or Codex auth mount.
- The only City operation is `gc --city "$CITY_PATH" sling mayor <text> --on superpowers-build --json`.
- The role file is mode 0600 and contains only launcher-side Agent Mail credentials.
- Persist `authorization_id → run_id` before acknowledging source Mail.
- Duplicate authorization resends its same binding and never starts another City workflow.

### Task 1: Add the private service and role file

**Files:**
- Modify: `Dockerfile.city`, `compose.yaml`, `scripts/gitea-mail-bridge-bootstrap.sh`, `Makefile`, `scripts/tests/test_gitea_mail_bridge.sh`
- Create: `scripts/city-mail-launcher.sh`

**Interfaces:** Bootstrap produces `state/city-mail-secrets/launcher.env`; Compose mounts it read-only at `/run/secrets/city-mail/launcher.env` and persists the launcher ledger at `/var/lib/city-mail-launcher/ledger.json`.

- [ ] **Step 1: Write the failing structural test**

Add `service_block city-mail-launcher "$launcher"` and require:

```sh
require '^  city-mail-launcher:$' "$launcher"
require 'target: /run/secrets/city-mail/launcher.env' "$launcher"
require 'read_only: true' "$launcher"
require 'target: /var/lib/city-mail-launcher' "$launcher"
```

Reject `ports:`, `GITEA_`, `GASCITY_API_URL`, `MCP_AGENT_MAIL_MAYOR`, and `CODEX_AUTH_FILE` in that block. Require `city-mail-secrets/launcher.env` in bootstrap and `gitea-mail-launcher-up` in Makefile.

- [ ] **Step 2: Verify red**

Run `bash scripts/tests/test_gitea_mail_bridge.sh`; expect failure because the launcher service is absent.

- [ ] **Step 3: Implement the service**

Add a `launcher-runtime` target to `Dockerfile.city` that copies `gc`, `bd`, `cagent`, and Dolt from existing build stages, installs `bash ca-certificates curl jq python3 tini`, copies the launcher script, and sets:

```dockerfile
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/city-mail-launcher"]
```

Add `city-mail-launcher` under profile `gitea-mail-bridge`, with `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`, no ports, City/rig/GC runtime mounts, launcher role-file mount read-only, and durable state mount. It depends on healthy `mcp-agent-mail` and `gitea-mail-bridge`.

Bootstrap atomically writes `launcher.env` with `MCP_AGENT_MAIL_BEARER_TOKEN`, `MCP_AGENT_MAIL_PROJECT_KEY`, `MCP_AGENT_MAIL_AGENT_NAME`, and `MCP_AGENT_MAIL_REGISTRATION_TOKEN`; it creates mode-0700 launcher state owned by host UID/GID. Add `gitea-mail-launcher-up` to bootstrap then start the service.

- [ ] **Step 4: Verify green**

Run `bash scripts/tests/test_gitea_mail_bridge.sh && docker compose --env-file .env.example --profile gitea-mail-bridge config -q`; expect success.

- [ ] **Step 5: Commit**

Run `git add Dockerfile.city compose.yaml scripts/gitea-mail-bridge-bootstrap.sh Makefile scripts/tests/test_gitea_mail_bridge.sh scripts/city-mail-launcher.sh && git commit -m 'feat: add private City mail launcher service'`.

### Task 2: Implement record-before-ack launch processing

**Files:**
- Create: `scripts/city_mail_launcher.py`, `scripts/tests/test_city_mail_launcher.py`
- Modify: `scripts/city-mail-launcher.sh`, `Makefile`

**Interfaces:** The helper accepts a decoded `gc.intake.start-authorized.v1` message and produces `gc.run.binding.v1` containing the original issue, plan, authorization ID, pinned base, and City `bead_id` as `run_id`.

- [ ] **Step 1: Write failing unit tests**

Create tests for these calls:

```python
self.assertEqual("auth-1", validate_authorization(valid_message)["payload"]["id"])
with self.assertRaisesRegex(ValueError, "pinned_base"):
    validate_authorization({"type": "gc.intake.start-authorized.v1", "payload": {"id": "auth-1"}})
self.assertEqual("bead-9", binding_for(valid_message, "bead-9")["payload"]["run_id"])
```

Test that a duplicate ledger mapping does not invoke the sling callback and that ledger persistence failure leaves acknowledgement uncalled.

- [ ] **Step 2: Verify red**

Run `PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_city_mail_launcher.py`; expect import failure.

- [ ] **Step 3: Implement minimal helper and daemon**

Implement `validate_authorization`, `binding_for`, and `record_before_ack` in Python. Validation requires the exact type, nonempty authorization ID, issue repository/number, plan ID, and a lowercase 40–64 hexadecimal pinned base. `record_before_ack` writes mode-0600 JSON through a sibling temporary file and `os.replace`, then calls acknowledgement.

The shell daemon loads the launcher role file, fetches only its own inbox, accepts only `gc.intake.start-authorized.` subjects, and invokes exactly:

```sh
gc --city "$CITY_PATH" sling mayor "$launch_text" --on superpowers-build --json
```

Extract `.bead_id`; send the derived binding to `gitea-mail-bridge`; then persist and acknowledge. On duplicate, resend the recorded binding without calling `gc sling`. On any failure, do not acknowledge.

- [ ] **Step 4: Verify green**

Run `PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_city_mail_launcher.py && sh -n scripts/city-mail-launcher.sh`; expect success.

- [ ] **Step 5: Commit**

Run `git add scripts/city-mail-launcher.sh scripts/city_mail_launcher.py scripts/tests/test_city_mail_launcher.py Makefile && git commit -m 'feat: bind City launches to intake authorizations'`.

### Task 3: Add a real-launcher acceptance driver

**Files:**
- Create: `scripts/gitea-mail-launcher-smoke.sh`
- Modify: `Makefile`, `scripts/tests/test_gitea_mail_bridge.sh`, `README.md`

**Interfaces:** A new disposable Gitea issue receives an authenticated plan and human approval; the driver waits for a non-synthetic City run binding in the bridge ledger.

- [ ] **Step 1: Write the failing structural test**

Require `gitea-mail-launcher-smoke` in Makefile and require the script success line:

```text
PASS: real City launcher fixture issue #N produced immutable City run binding
```

Require the script to invoke `gitea-mail-launcher-up`, inspect bridge ledger, and reject `smoke-run-` IDs.

- [ ] **Step 2: Verify red**

Run `bash scripts/tests/test_gitea_mail_bridge.sh`; expect failure because the driver is absent.

- [ ] **Step 3: Implement the driver**

Reuse the existing smoke's issue creation, signed ingress, Mayor plan, and human-approval helpers. Do not fabricate a launcher binding. Instead wait for a bridge ledger binding matching the authorization, require a nonempty non-`smoke-run-` run ID and exact pinned base, then close only the disposable issue. Preserve City run and ledger state. Document that this covers real-launch evidence only; amendment/reapproval and public Mayor formula evidence remain Gate D subtraces.

- [ ] **Step 4: Verify green**

Run `bash scripts/tests/test_gitea_mail_bridge.sh && make test`; expect success.

- [ ] **Step 5: Commit**

Run `git add scripts/gitea-mail-launcher-smoke.sh Makefile scripts/tests/test_gitea_mail_bridge.sh README.md && git commit -m 'test: add real City launcher smoke driver'`.

### Task 4: Verify, review, deploy

- [ ] **Step 1: Run complete local verification**

Run:

```sh
make test
docker compose --env-file .env.example --profile gitea-mail-bridge config -q
docker compose --env-file .env --profile gitea-mail-bridge config -q
git diff --check
```

Expect all commands to exit 0.

- [ ] **Step 2: Inspect rendered authority boundary**

Run `docker compose --env-file .env --profile gitea-mail-bridge config | sed -n '/^  city-mail-launcher:/,/^  [a-zA-Z0-9_-]\\+:/p'`; expect no port, Gitea token, webhook secret, Mayor token, or City HTTP URL.

- [ ] **Step 3: Push and review**

Run `git push -u origin feat/gate-d-launcher`, open a PR, and wait for hosted validation to pass before merge.

- [ ] **Step 4: Deploy and run acceptance**

After merge, run `make gitea-mail-launcher-up ENV_FILE=.env` and `make gitea-mail-launcher-smoke ENV_FILE=.env`. Require Gate B readiness plus exactly one real City run binding recorded before bridge acknowledgement.
