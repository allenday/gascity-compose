# SDD ledger — plan: docs/superpowers/plans/2026-09-04-github-durable-gateway.md

| Task(s) | Shared interface/file | Pre-flight finding | Ruling |
| --- | --- | --- | --- |
| 1 + 2 | `scripts/github_durable_gateway.py` | Task 1 owns store/leases; Task 2 adds HTTP and worker orchestration. | Keep the store API dependency-free and test it without HTTP. |
| 2 + 3 | `scripts/github_docs_impact_webhook.py`, `compose.yaml` | Task 2 changes ingress behavior; Task 3 moves its service topology. | Retain the existing webhook path/name during Task 2; rename/wire service only in Task 3. |
| 3 + 4 | Compose state volume and smoke scripts | Task 4 assumes gateway state survives City restart. | Persist SQLite under the existing `state/github-intake` mount. |
| Task 1 | Task text | Store uses only stdlib and has no Pack ownership conflict. | Clean. |
| Task 2 | Task text | Job kinds exceed the existing one-shot webhook shape. | Use a bounded four-kind queue, each idempotently calls existing adapter behavior. |
| Task 3 | Task text | Existing Nginx targets `github-webhook`. | Preserve that DNS service name for the new independent gateway to avoid unrelated Nginx change. |
| Task 4 | Task text | Existing real GitHub event replay is unreliable in dev. | Build deterministic local restart smoke first; external dogfood is a second verification layer. |

Task 1: fix round 1/5 (stale lease race addressed; commits 54b8d64..198c863)
Task 1: complete (commits 70ab17f..198c863, review clean)
Task 2: fix round 1/5 (durable stage predicate addressed; commits 07d4ff6..b609169)
Task 2: complete (commits 198c863..b609169, review clean)
Task 3: Ruling: retain the Compose/Nginx service key and `GC_SERVICE_NAME` `github-webhook` — it is the durable gateway's stable endpoint; renaming costs an unrelated proxy change.
Task 3: complete (commit b609169..36e4bcf, review clean)
Task 4: fix round 1/5 (persisted follow-up replay evidence; commits c46f1f4..4cd186a)
Task 4: fix round 2/5 (production source-branch mapping coverage; commits 4cd186a..14a1143)
Task 4: local restart acceptance complete (commits 36e4bcf..14a1143); external GitHub dogfood remained unexecuted.
Final whole-branch review: changes requested at 14a1143 (malformed input, live health/progress, Compose least privilege, restart topology proof, retry clock, and incomplete ledger).
Final review fix round 1/5: complete in this commit. Safe local acceptance now exercises the actual Compose `github-webhook` and `city` service lifecycle with deterministic filesystem boundaries; no external GitHub mutation was performed.
