# City Gitea Onboarding Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver fail-closed City-owned Gitea repository onboarding and triage-gated intake authorization.

**Architecture:** Compose owns a manifest, read-only doctor, and derived provisioning inputs. The Gitea controller consumes the derived fixed-role identity set and provider permission evidence. The linked parent validates their combined behavior.

**Tech Stack:** Shell, Docker Compose, Python tests, Go controller tests, Gitea REST API.

**Spec:** `docs/superpowers/specs/2026-08-26-city-gitea-onboarding-policy.md`

## Global Constraints

- Intake repositories are exact, private/internal, and have no issue history.
- Fixed roles are Mayor, bridge, and launcher; all are restricted/non-admin and distinct.
- Human assignment and approval require current non-City `triage` or stronger access.
- The bridge never receives City-run authority or a public port.

---

### Task 1: Compose manifest, doctor, and bootstrap derivation

**Files:**
- Modify: `compose.yaml`, `.env.example`, `scripts/gitea-mail-bridge-bootstrap.sh`
- Create: `scripts/gitea-intake-doctor.sh`
- Test: `scripts/tests/test_gitea_mail_bridge.sh`

- [ ] Write structural tests for manifest-only scopes, fixed role names, read-only empty-history checks, and clear failure messages.
- [ ] Run the structural test and confirm it fails before implementation.
- [ ] Implement manifest parsing and doctor checks; derive scopes and City identities; reconcile fixed accounts and repo permissions.
- [ ] Run `make test` and Compose config validation.
- [ ] Commit and open a PR referencing Compose #37.

### Task 2: Controller triage authorization

**Files:**
- Modify: `intake/types.go`, `intake/decision.go`, Gitea reconciliation/provider adapter files
- Test: `intake/decision_test.go`, reconciliation tests

- [ ] Write failing tests for triage assignment/approval success and read/outsider/City rejection.
- [ ] Run focused Go tests and confirm failure.
- [ ] Persist provider permission evidence needed for current authorization and implement fail-closed policy evaluation.
- [ ] Run `go vet ./...` and `go test ./... -race -count=1`.
- [ ] Commit and open a PR referencing Gitea #42.

### Task 3: Integrated acceptance

**Files:**
- Modify: Compose acceptance/doctor fixture and Gitea integration tests as required

- [ ] Add a clean-repository onboarding fixture and a repository-removal check.
- [ ] Run private bootstrap, controller, and live fixture verification without exposing secrets.
- [ ] Capture redacted logs, ledger projections, and checksums.
- [ ] Record evidence in #43 only after #37 and #42 have closed.
