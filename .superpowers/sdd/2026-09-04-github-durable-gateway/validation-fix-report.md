# Validation fix report: durable GitHub gateway

## Status

The signed payload validator now rejects malformed identity field types before
the webhook handler can enqueue a delivery. Installation, repository, pull
request, base/head repository, ref, and SHA fields must have the scalar JSON
types and value shapes expected from GitHub; installation, repository, and PR
IDs must be positive integers, repository names must be `owner/repository`,
refs must be nonempty strings, and commit SHAs must be 40 hexadecimal
characters.

Two regression tests cover `installation.id: [23]`:

- A signed HTTP delivery and its replay both return `400 invalid_payload` and
  leave no SQLite delivery or job rows.
- A legacy persisted delivery with the same malformed field is terminalized
  without invoking the adapter, so it cannot become retryable work.

## Verification

Passed after the test-first failure was observed:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/tests/test_github_durable_gateway.py
git diff --check
```

Result: 18 gateway tests passed; the diff check completed without output.

## Scope

No Compose topology or external GitHub state was changed. Pre-existing
untracked Python bytecode directories remain untouched.
