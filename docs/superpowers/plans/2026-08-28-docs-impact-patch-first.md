# Docs-impact patch-first implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a revision-bound TechDocs patch artifact without writing to a customer pull request branch.

**Architecture:** A tokenless worker creates a canonical proposal. The trusted GitHub supervisor validates, persists, and projects it as an actionable Check Run; a new source SHA always evaluates independently.

**Tech Stack:** Python 3 standard library, Gas City beads, GitHub Check Runs, unittest.

**Spec:** `docs/superpowers/specs/2026-08-28-docs-impact-patch-first-design.md`

## Global Constraints

- Never push branches or open PRs in this slice.
- Preserve repository ID, PR number, and exact-head SHA identity.
- Reject unsafe proposals and redact secrets.
- Every result remains `ACTION_REQUIRED`.

### Task 1: Canonical artifact validation

**Files:** Create `github/scripts/github_intake_docs_patch.py`; create `github/tests/test_github_intake_docs_patch.py`.

- [ ] Write failing tests for deterministic digest, traversal/non-doc/binary/oversize rejection, missing evidence, and redaction.
- [ ] Run `python3 -m unittest github.tests.test_github_intake_docs_patch` and verify failure.
- [ ] Implement strict schema, digest, safe path/diff, claim-ledger, and redaction validation.
- [ ] Re-run the focused suite and commit `feat(github): validate docs patch artifacts`.

### Task 2: Immutable result projection

**Files:** Modify `github/scripts/github_intake_service.py` and `github/scripts/github_intake_docs_impact.py`; modify `github/tests/test_github_intake_docs_impact.py`.

- [ ] Write failing tests for derived-bead idempotency, source-SHA invalidation, safe Check Run text, and unavailable output.
- [ ] Implement artifact persistence and key `github-pr-docs-patch:{repo_id}:{pr}:{head_sha}:{digest}` without changing source-bead semantics.
- [ ] Run `python3 -m unittest discover -s github/tests -p 'test_*.py'` and commit `feat(github): project docs patch proposals`.

### Task 3: Worker isolation and dogfood

**Files:** Modify `config/github-intake/rules.toml`, `compose.yaml`, `scripts/tests/test_github_docs_impact.sh`, and `github/README.md`.

- [ ] Write failing structural tests proving the worker receives no GitHub token, no writable city/config mount, and has no published port.
- [ ] Add a tokenless sanitized-snapshot worker and keep Check Run publication in the trusted supervisor.
- [ ] Run `make test`, full GitHub tests, and `git diff --check`; commit `feat(compose): isolate docs patch worker`.
- [ ] Dogfood a replay: verify source bead, result bead, actionable check, and no branch mutation; record #54 CI/acceptance evidence after independent critique.
