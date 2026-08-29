#!/bin/sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
compose="$root/compose.yaml"
rules="$root/config/github-intake/rules.toml"

require() {
  local pattern=$1 file=$2
  grep -Eq "$pattern" "$file" || { echo "missing $pattern in $file" >&2; exit 1; }
}

require 'github-webhook:' "$compose"
require 'github-admin:' "$compose"
require 'github-docs-patch-worker:' "$compose"
require 'profiles: \[github-docs-impact\]' "$compose"
require 'GC_SERVICE_HOST: 0.0.0.0' "$compose"
require 'GC_SERVICE_PORT: "8080"' "$compose"
require 'HOME: /var/lib/github-intake/home' "$compose"
require 'GITHUB_WEBHOOK_WAN_IP:-127.0.0.1' "$compose"
require 'GITHUB_WEBHOOK_WAN_PORT:-8088' "$compose"
require 'city:8080' "$root/nginx/nginx.conf"
require 'city:8081' "$root/nginx/nginx.conf"
require 'location = /v0/github/webhook' "$root/nginx/nginx.conf"
require 'location = /v0/github/admin' "$root/nginx/nginx.conf"
require 'location \^~ /v0/github/admin/' "$root/nginx/nginx.conf"
require 'location \^~ /v0/github/app/' "$root/nginx/nginx.conf"
require 'action = "opened"' "$rules"
require 'action = "reopened"' "$rules"
require 'action = "synchronize"' "$rules"
require 'action = "ready_for_review"' "$rules"
require 'github_intake_docs_impact_pipeline.py' "$rules"
require 'github_app_token_env = "GH_TOKEN"' "$rules"
require 'libicu72' "$root/Dockerfile.city"
require 'BEADS_VERSION=v1.1.1-0.20260805093327-bf97b73749ac' "$root/Dockerfile.city"

# The profile service deliberately has no `ports:` declaration. Its only
# reachable path is Nginx, which can then be scoped by Tailscale Funnel.
if awk '/^  github-webhook:/{inside=1; next} inside && /^  [^ ]/{exit} inside && /^    ports:/{found=1} END{exit !found}' "$compose"; then
  echo 'github-webhook must not publish a host port' >&2
  exit 1
fi

worker_block=$(awk '/^  github-docs-patch-worker:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-patch-worker:/{exit}' "$compose")
[ -n "$worker_block" ] || { echo 'github-docs-patch-worker service block is missing' >&2; exit 1; }
webhook_block=$(awk '/^  github-webhook:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-webhook:/{exit}' "$compose")
[ -n "$webhook_block" ] || { echo 'github-webhook service block is missing' >&2; exit 1; }
admin_block=$(awk '/^  github-admin:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-admin:/{exit}' "$compose")
[ -n "$admin_block" ] || { echo 'github-admin service block is missing' >&2; exit 1; }
reviewer_block=$(awk '/^  github-docs-techdocs-reviewer:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-techdocs-reviewer:/{exit}' "$compose")
egress_block=$(awk '/^  github-docs-model-egress:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-model-egress:/{exit}' "$compose")
city_block=$(awk '/^  city:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  city:/{exit}' "$compose")
[ -n "$city_block" ] || { echo 'city service block is missing' >&2; exit 1; }
[ -z "$reviewer_block" ] || { echo 'legacy cagent reviewer service must be removed' >&2; exit 1; }
[ -z "$egress_block" ] || { echo 'legacy OpenAI model egress service must be removed' >&2; exit 1; }

require_webhook() {
  pattern=$1
  printf '%s\n' "$webhook_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-webhook" >&2
    exit 1
  }
}

require_admin() {
  pattern=$1
  printf '%s\n' "$admin_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-admin" >&2
    exit 1
  }
}

require_worker() {
  pattern=$1
  printf '%s\n' "$worker_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-docs-patch-worker" >&2
    exit 1
  }
}

forbid_worker() {
  pattern=$1
  if printf '%s\n' "$worker_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in github-docs-patch-worker" >&2
    exit 1
  fi
}

# The untrusted producer gets only the supervisor-created sanitized snapshot
# plus a dedicated artifact outbox. It cannot receive App credentials, City
# state/configuration, or a network listener.
require_worker 'GC_TECHDOCS_SNAPSHOT_DIR: /work/snapshot'
require_worker 'GC_TECHDOCS_ARTIFACT_DIR: /work/artifact'
require_worker 'GC_TECHDOCS_ADAPTER_COMMAND:.*GC_TECHDOCS_ADAPTER_COMMAND:-'
require_worker 'GC_TECHDOCS_ADAPTER_TIMEOUT_SECONDS:.*GC_TECHDOCS_ADAPTER_TIMEOUT_SECONDS:-300'
require_worker 'GC_TECHDOCS_SKILL_DIR: /opt/gascity-packs/github/skills/developer-experience-techdocs'
require_worker 'github_intake_docs_patch_queue_worker.py'
require_worker 'test -f "\$\$worker"'
require_worker 'GitHub pack lacks github_intake_docs_patch_queue_worker.py'
require_worker 'docs-patch-snapshots.*:/work/snapshot:ro'
require_worker 'docs-patch-artifacts.*:/work/artifact'
require_worker 'read_only: true'
require_worker 'network_mode: "none"'
require_worker 'cap_drop:'
require_worker 'restart: unless-stopped'
forbid_worker 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'
forbid_worker '\$\{CITY_DIR'
forbid_worker './config/github-intake'
forbid_worker '^    ports:'

require_city() {
  pattern=$1
  printf '%s\n' "$city_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in city" >&2
    exit 1
  }
}

forbid_city() {
  pattern=$1
  if printf '%s\n' "$city_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in city" >&2
    exit 1
  fi
}

# Codex authentication stays in the already-trusted City runtime. The City
# queue launcher sees sanitized snapshots read-only, creates immutable local
# copies, and returns digest-bound candidates to the networkless validator.
require_city 'CODEX_AUTH_FILE.*:/run/secrets/codex-auth.json:ro'
require_city 'GC_CITY_DOCS_REVIEW_ENABLED:.*true'
require_city 'GC_CITY_DOCS_REVIEW_TARGET: github-docs-impact.docs-impact-reviewer'
require_city 'docs-patch-snapshots.*:/var/lib/github-docs-impact/snapshot:ro'
require_city 'docs-patch-candidates.*:/var/lib/github-docs-impact/candidate'
require_city 'docs-review-immutable.*:/var/lib/github-docs-impact/immutable'
require_city 'docs-review-dispatch.*:/var/lib/github-docs-impact/dispatch'
require_city 'GITHUB_PACK_DIR.*:/opt/gascity-packs:ro'
forbid_city 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'

require 'github_intake_city_docs_launcher.py' "$root/docker/city-entrypoint.sh"
require '/opt/gascity-packs/github' "$root/docker/city-entrypoint.sh"
require '^\[providers\.codex-docs-impact\]$' "$root/config/city-cost-safe.toml"
require 'gpt-5\.6-terra' "$root/config/city-cost-safe.toml"
require 'model_reasoning_effort=medium' "$root/config/city-cost-safe.toml"

if [ "$(grep -Fc '/run/secrets/codex-auth.json' "$compose")" -ne 1 ]; then
  echo 'Codex auth must be mounted only into the trusted City service' >&2
  exit 1
fi
if grep -Eq 'GC_TECHDOCS_MODEL_(TOKEN|ENDPOINT)|github_docs_model_egress_proxy|github_intake_city_techdocs_adapter|github-docs-model' "$compose"; then
  echo 'legacy OpenAI API-key/cagent reviewer route must be absent' >&2
  exit 1
fi

# One profile activates the complete trusted review path. City remains
# profile-gated, so this does not weaken the one-supervisor-at-a-time guard.
require '^    profiles: \[city, github-docs-impact\]$' "$compose"
profile_services=$(docker compose --env-file "$root/.env.example" --profile github-docs-impact config --services)
for expected in city github-webhook github-admin github-docs-patch-worker; do
  printf '%s\n' "$profile_services" | grep -Fx "$expected" >/dev/null || {
    echo "github-docs-impact profile does not activate $expected" >&2
    exit 1
  }
done

# The trusted rule subprocess shares the exact queue directories, waits longer
# than the worker's default adapter timeout, and alone receives the token used
# by the pipeline's projector/publisher.
require_webhook 'GC_GITHUB_DOCS_PATCH_SNAPSHOT_DIR: /var/lib/github-intake/docs-patch-snapshots'
require_webhook 'GC_GITHUB_DOCS_PATCH_ARTIFACT_DIR: /var/lib/github-intake/docs-patch-artifacts'
require_webhook 'GC_GITHUB_DOCS_REVIEW_WAIT_SECONDS:.*GC_GITHUB_DOCS_REVIEW_WAIT_SECONDS:-305'
require_webhook 'network_mode: "service:city"'
require_webhook 'GC_HOME: /root/.gc'
require_webhook 'state/gc-runtime:/root/.gc'
require_webhook 'GC_GITHUB_INTAKE_DIRECT_BD: "1"'
require_webhook 'BEADS_DIR:.*CITY_DIR.*\.beads'
require_admin 'network_mode: "service:city"'
