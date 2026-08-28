# Docs-impact patch-first design

## Outcome

For a GitHub pull request whose exact revision needs documentation work, Gas City produces a safe, evidence-backed documentation patch without requiring permission to write to the pull request branch. The required `Gas City / docs-impact` check remains `ACTION_REQUIRED` until a later pull request revision is independently evaluated with sufficient documentation evidence.

## Scope

This design implements issue [#54](https://github.com/allenday/gascity-compose/issues/54) only. Same-branch writeback and companion documentation pull requests consume the artifact defined here but are separate delivery issues.

## Architecture

The current immutable source bead remains the code-revision identity:

`github-pr:{repository_id}:{pr_number}:{head_sha}`

The evaluator creates a distinct immutable derived result bead for every generated patch:

`github-pr-docs-patch:{repository_id}:{pr_number}:{head_sha}:{patch_sha256}`

It also persists a canonical JSON artifact keyed by the same digest. The untrusted TechDocs worker receives only a sanitized PR snapshot and creates the artifact; it has no GitHub token, git credentials, or writable city/config mounts. A trusted supervisor validates the strict artifact schema and is the only component that can publish a GitHub Check Run.

## Artifact contract

The canonical JSON document has `schema_version: 1` and includes:

- code identity: repository ID/name, PR number, base SHA, head SHA, head repository ID/name, and base ref;
- `patch_sha256` and a bounded unified diff against the recorded head SHA;
- changed documentation paths and each resulting file SHA-256;
- a TechDocs claim ledger: claim, immutable evidence reference, and release scope;
- repository-native documentation check commands and one of `passed`, `failed`, or `unavailable` with an explanation;
- `proposed`, `unavailable`, or `unsafe` artifact status and an RFC3339 generation time.

The supervisor rejects absolute or traversal paths, non-documentation paths, binary or symlink changes, invalid hashes, oversized diffs, missing evidence, and any patch that does not apply to the recorded head SHA. It redacts tokens and headers from every persisted/public field.

## Check behavior

For a valid proposal, the check is completed as `ACTION_REQUIRED` with title `Documentation update proposed`. Its summary names the immutable source bead, source key, code SHA, artifact digest, and application instruction. A bounded safe diff is placed in Check Run text. An artifact is never success evidence by itself.

For an unavailable or unsafe proposal, the check is completed `ACTION_REQUIRED` with the reason and no patch text. Any source PR `synchronize` creates a fresh code-revision source bead and evaluates that SHA without inheriting an earlier result.

## Customer and fork safety

Patch-first is the default regardless of App permissions. Fork-originated and no-write PRs are supported because no branch write is attempted. This slice neither calls the existing branch-push helper nor grants its GitHub token to TechDocs work.

## Acceptance evidence

Tests must prove deterministic result identity; duplicate-delivery idempotency; SHA invalidation; safe check output; rejection of traversal, non-doc, binary, oversized, and non-applying patches; redaction; and `ACTION_REQUIRED` behavior for every proposal. Existing exact-head source/check tests must remain green.
