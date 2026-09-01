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
mkdir -p "$pack/github/scripts" "$pack/github/agents/docs-impact-reviewer" "$city" "$rig" "$source" "$state/data"
: > "$pack/github/scripts/github_intake_docs_review_runtime.py"
: > "$pack/github/scripts/github_intake_docs_impact.py"
: > "$pack/github/agents/docs-impact-reviewer/prompt.template.md"
: > "$auth"

env_file="$temp/github.env"
cat > "$env_file" <<EOF
CITY_DIR=$city
MY_PROJECT_DIR=$rig
GASCITY_SOURCE_DIR=$source
GC_CITY_DOCS_REVIEW_RIG_DIR=$source
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

ENV_FILE="$env_file" sh "$script"

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
ENV_FILE="$env_file" sh "$script"
