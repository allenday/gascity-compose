# City-Owned Gitea Onboarding Policy

## Goal

Allow a City to onboard only explicitly declared, private/internal, issue-empty Gitea repositories, with all Gitea authority derived from fixed City roles and current Gitea permissions.

## Policy

- A City manifest declares exact `owner/repo` intake repositories and `triage` as the minimum human authority.
- A read-only doctor rejects a repository with any open or closed issue, non-private/non-internal visibility, missing access, or a role account that cannot be created or managed.
- Bootstrap creates or verifies exactly `gascity-mcp-mayor`, `gascity-mail-bridge`, and `gascity-mail-launcher`; they are distinct, restricted, non-admin accounts.
- The manifest is the source of repository scopes and City identities. Compose does not accept manually maintained human-eligibility or City-identity authority lists.
- The controller accepts Mayor assignment and approval only from a current non-City Gitea collaborator with `triage` or stronger permission. Approval must still follow the current authenticated Mayor plan.
- A removed repository stops new webhook/reconciliation intake. Durable ledger history and Gitea resources are retained.
- Comments, mentions, and activity on unmanaged issues never create intake.

## Components

`gascity-compose` owns manifest parsing, doctor output, role provisioning, bootstrap rendering, webhook/label reconciliation, and removal behavior. `gascity-gitea` owns policy representation plus assignment/approval authorization using provider-observed permission.

The bridge continues to have read-only Gitea and private Mail authority only. The Mayor retains scoped Gitea write authority; the launcher remains limited to returned immutable bindings.

## Acceptance

1. A fresh private/internal issue-empty repo onboards idempotently.
2. A repo with any issue fails the doctor before mutation.
3. Missing/incorrect fixed role accounts or permissions fail with actionable diagnostics.
4. Non-City `triage` users can assign/approve; read users, outsiders, and City identities cannot.
5. A manifest removal prevents new intake while retaining durable history.
6. Existing replay, cursor, plan-provenance, approval-ordering, and record-before-ack checks remain green.
