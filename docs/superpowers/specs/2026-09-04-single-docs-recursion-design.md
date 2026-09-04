# Single documentation recursion design

## Goal

Use one documentation recursion for every incoming context. A pull request, a
GitHub issue, and an explicit operator request differ only in their immutable
context and declared documentation coverage collection.

The City is a dark factory. GitHub is the human-facing record of what the
recursion found and what it did: checks, follow-up pull requests, and deferred
issues.

## Proposed flow

```mermaid
flowchart TD
  IN[Incoming context<br/>PR revision, issue, or operator request] --> SNAP[Persist immutable context<br/>and declared coverage collection]
  SNAP --> ASSESS[City assesses each affected<br/>persona-goal-doc-type cell]
  ASSESS --> DECIDE{Cell result}

  DECIDE -->|sufficient| PASS[Project passing GitHub check<br/>or terminal report]
  DECIDE -->|human judgment required| REVIEW[Project action-required<br/>GitHub check or issue]
  DECIDE -->|safe active coverage| WORK[Persist bounded City child work]
  WORK --> RESULT[Validate worker result]
  RESULT --> PR[Project one bot-owned<br/>follow-up PR]
  RESULT --> REVIEW

  DECIDE -->|uncovered cell| BUD[Persist evidence-backed bud]
  BUD --> ISSUE[Create or update one idempotent<br/>GitHub issue]
  ISSUE -->|explicit human activation| IN

  PR -->|merge creates new revision| IN
```

## Rules

- Every run has one immutable incoming context and a declared coverage
  collection of persona, goal, and documentation-type cells. The collection
  may contain one cell or the full relevant Diataxis matrix.
- The City records a result for every affected cell: sufficient, covered by
  active work, or deferred as a bud.
- Execution has explicit limits for depth, active child work items, elapsed
  time, and non-progress. Bud creation is not execution and never consumes
  those limits.
- Every uncovered cell becomes an evidence-backed bud. The App creates or
  updates one idempotent GitHub issue for it, carrying the current context and
  evidence.
- A bud is inert. Creating its issue does not start another run. A human
  activates it through an explicit policy, such as an assigned label or a
  command comment; that activation supplies a new incoming context.
- A follow-up pull request is also a projection, not a separate workflow.
  Merging it supplies a new pull-request revision and starts the next bounded
  traversal.

## PR active-work limits

A pull-request revision is expected to be shallow, but it uses the same
recursion. Its default active-work limit permits one documentation child and
one follow-up pull request. That limit never suppresses a bud: every
uncovered affected cell is still projected to its deduplicated issue. It does
not automatically activate any issue buds.

```mermaid
flowchart LR
  PR[PR revision] --> ASSESS[Assess affected coverage cells]
  ASSESS -->|sufficient cells| PASS[Pass]
  ASSESS -->|inconclusive cells| HUMAN[Human review]
  ASSESS -->|safe docs subset| CHILD[One City child]
  CHILD --> FOLLOWUP[One follow-up PR]
  FOLLOWUP -->|merged| PR
  ASSESS -->|uncovered cells| BUDS[Create or update<br/>one issue per cell]
```

## Refactor boundary

The Compose integration should retain the signed durable gateway, SHA-bound
assessment, credential-free City workers, and GitHub App publisher.

It should remove the generic documentation-journey controller from the
pull-request path. In particular, the pull-request path must not create a
GitHub issue and a second Bead merely to dispatch the child work already
identified by the assessment.

The recursion record is the only lifecycle authority. City Beads are its
execution detail; GitHub issues and pull requests are its human-visible
projections.
