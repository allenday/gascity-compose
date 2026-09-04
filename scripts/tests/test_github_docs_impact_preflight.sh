#!/bin/sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
script="$root/scripts/github-docs-impact-preflight.sh"
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT

pack="$temp/packs"
city="$temp/city"
rig="$temp/rig"
source="$temp/source"
auth="$temp/auth.json"
state="$temp/github-intake"
mkdir -p "$pack/github/scripts" "$pack/github/agents/docs-impact-reviewer" "$pack/github/agents/docs-journey" "$city" "$rig" "$source" "$state/data"
: > "$pack/github/scripts/github_intake_docs_review_runtime.py"
: > "$pack/github/scripts/github_intake_docs_impact.py"
: > "$pack/github/scripts/github_intake_docs_journey_commands.py"
: > "$pack/github/scripts/github_intake_docs_direct_child_admit.py"
: > "$pack/github/scripts/github_intake_docs_direct_child_complete.py"
: > "$pack/github/agents/docs-impact-reviewer/prompt.template.md"
: > "$pack/github/agents/docs-journey/prompt.template.md"
mkdir -p "$pack/github/agents/docs-recursion-direct-child"
: > "$pack/github/agents/docs-recursion-direct-child/prompt.template.md"
: > "$pack/github/agents/docs-recursion-direct-child/agent.toml"
: > "$auth"

env_file="$temp/github.env"
cat > "$env_file" <<EOF
CITY_DIR=$city
MY_PROJECT_DIR=$rig
GASCITY_SOURCE_DIR=$source
GC_CITY_DOCS_REVIEW_RIG_DIR=$source
GC_CITY_DOCS_REVIEW_TARGET=source/github-docs-impact.docs-impact-reviewer
GC_CITY_DOCS_DIRECT_CHILD_TARGET=source/github-docs-impact.docs-recursion-direct-child
CODEX_AUTH_FILE=$auth
CITY_MAIL_LAUNCHER_RIG=my-project
STACK_PASSWORD=test-password
TAILNET_IP=100.64.0.1
BUZZ_RELAY_URL=ws://100.64.0.1:3003
BUZZ_MEDIA_BASE_URL=http://100.64.0.1:3003/media
BUZZ_MEDIA_SERVER_DOMAIN=100.64.0.1:3003
BUZZ_CORS_ORIGINS=http://100.64.0.1:3003
BUZZ_RELAY_PRIVATE_KEY=test-relay-key
BUZZ_RELAY_OWNER_PUBKEY=0000000000000000000000000000000000000000000000000000000000000000
BUZZ_GIT_HOOK_HMAC_SECRET=test-hook-secret
BUZZ_POSTGRES_PASSWORD=test-postgres-password
BUZZ_REDIS_PASSWORD=test-redis-password
BUZZ_S3_ACCESS_KEY=test-access-key
BUZZ_S3_SECRET_KEY=test-secret-key
BUZZ_PUBLIC_RELAY_URL=http://100.64.0.1:3003
BUZZ_MAYOR_BRIDGE_RELAY_URL=http://buzz-relay:3000
GASCITY_GITEA_REF=test-ref
GITHUB_PACK_DIR=$pack
GITHUB_APP_ID=4748619
GITHUB_WEBHOOK_SECRET=test-secret
GITHUB_APP_PRIVATE_KEY_PEM=test-key
EOF

# A gateway must remain runnable when City is unavailable.  Prove preflight
# rejects the forbidden shared network namespace, rather than relying on YAML
# parsing to catch a duplicate key.
coupled_compose="$temp/coupled-compose.yaml"
python3 - "$coupled_compose" <<'PY'
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text(
    "services:\n  github-webhook:\n    network_mode: service:city\n",
    encoding="utf-8",
)
PY
if ENV_FILE="$env_file" COMPOSE_FILE="$root/compose.yaml:$coupled_compose" sh "$script" >"$temp/out" 2>&1; then
  echo 'preflight accepted github-webhook network_mode: service:city' >&2
  exit 1
fi
grep -q 'github-webhook must not use network_mode: service:city' "$temp/out"

ENV_FILE="$env_file" sh "$script" >"$temp/out"
grep -q 'runnable_jobs=0' "$temp/out"
grep -q 'oldest_runnable_job=none' "$temp/out"

rm "$pack/github/agents/docs-recursion-direct-child/agent.toml"
if ENV_FILE="$env_file" sh "$script" >"$temp/out" 2>&1; then
  echo 'preflight accepted a pack without the direct-child agent manifest' >&2
  exit 1
fi
grep -q 'github/agents/docs-recursion-direct-child/agent.toml' "$temp/out"
: > "$pack/github/agents/docs-recursion-direct-child/agent.toml"

sed -i 's#GITHUB_PACK_DIR=.*#GITHUB_PACK_DIR=relative-pack#' "$env_file"
if ENV_FILE="$env_file" sh "$script" >"$temp/out" 2>&1; then
  echo 'preflight accepted a relative GITHUB_PACK_DIR' >&2
  exit 1
fi
grep -q 'GITHUB_PACK_DIR must be an absolute path' "$temp/out"

# Imported GitHub App credentials are persisted in the protected intake state;
# a later Compose run must not require operators to duplicate them into .env.
sed -i 's#GITHUB_PACK_DIR=.*#GITHUB_PACK_DIR='"$pack"'#' "$env_file"
sed -i 's#GITHUB_APP_ID=.*#GITHUB_APP_ID=#' "$env_file"
sed -i 's#GITHUB_WEBHOOK_SECRET=.*#GITHUB_WEBHOOK_SECRET=#' "$env_file"
sed -i 's#GITHUB_APP_PRIVATE_KEY_PEM=.*#GITHUB_APP_PRIVATE_KEY_PEM=#' "$env_file"
printf '%s\n' 'GITHUB_INTAKE_STATE_ROOT='"$state" >> "$env_file"
printf '%s\n' '{"app":{"app_id":"4748619","webhook_secret":"test-secret","private_key_pem":"test-key"}}' > "$state/data/config.json"
python3 - "$state/gateway.sqlite" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, available_at INTEGER, lease_until INTEGER)")
    connection.executemany(
        "INSERT INTO jobs (id, status, available_at, lease_until) VALUES (?, ?, ?, ?)",
        (
            (7, "pending", 100, None),
            (8, "pending", 4_000_000_000, None),
            (9, "complete", 1, None),
            (10, "failed", 1, None),
        ),
    )
PY
ENV_FILE="$env_file" sh "$script" >"$temp/out"
grep -q 'runnable_jobs=1' "$temp/out"
grep -q 'oldest_runnable_job=7' "$temp/out"
