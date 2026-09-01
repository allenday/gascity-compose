#!/bin/sh
set -eu

root=$(cd "$(dirname "$0")/../.." && pwd)
compose="$root/compose.yaml"
rules="$root/config/github-intake/rules.toml"
ci_workflow="$root/.github/workflows/ci.yml"

require() {
  local pattern=$1 file=$2
  grep -Eq "$pattern" "$file" || { echo "missing $pattern in $file" >&2; exit 1; }
}

require 'github-webhook:' "$compose"
require 'github-docs-review-runtime:' "$compose"
require 'profiles: \[github-docs-impact\]' "$compose"
require 'GC_SERVICE_HOST: 0.0.0.0' "$compose"
require 'GC_SERVICE_PORT: "8080"' "$compose"
require 'HOME: /var/lib/github-intake/home' "$compose"
require 'GITHUB_WEBHOOK_WAN_IP:-127.0.0.1' "$compose"
require 'GITHUB_WEBHOOK_WAN_PORT:-8088' "$compose"
if grep -Fq '${GITHUB_PACK_DIR:-' "$compose"; then
  echo 'github docs-impact mounts must not fall back to a sibling pack checkout' >&2
  exit 1
fi
require 'GITHUB_PACK_DIR:\?Set GITHUB_PACK_DIR in \.env to the absolute GitHub pack checkout' "$compose"
# Keep CI's fixture environment aligned with compose's required reviewer
# inputs, so interpolation failures are caught before GitHub Actions runs.
require 'GC_CITY_DOCS_REVIEW_RIG_DIR: \$\{\{ github\.workspace \}\}/fixture-my-project' "$ci_workflow"
require 'GC_CITY_DOCS_REVIEW_TARGET: my-project/github-docs-impact\.docs-impact-reviewer' "$ci_workflow"
require 'city:8080' "$root/nginx/nginx.conf"
require 'location = /v0/github/webhook' "$root/nginx/nginx.conf"
require 'action = "opened"' "$rules"
require 'action = "reopened"' "$rules"
require 'action = "synchronize"' "$rules"
require 'action = "ready_for_review"' "$rules"
require 'github_docs_impact_compose_adapter.py", "intake", "--once' "$rules"
require 'github_app_token_env = "GH_TOKEN"' "$rules"
require 'libicu72' "$root/Dockerfile.city"
require 'BEADS_VERSION=v1.1.1-0.20260805093327-bf97b73749ac' "$root/Dockerfile.city"

# The profile service deliberately has no `ports:` declaration. Its only
# reachable path is Nginx, which can then be scoped by Tailscale Funnel.
if awk '/^  github-webhook:/{inside=1; next} inside && /^  [^ ]/{exit} inside && /^    ports:/{found=1} END{exit !found}' "$compose"; then
  echo 'github-webhook must not publish a host port' >&2
  exit 1
fi

runtime_block=$(awk '/^  github-docs-review-runtime:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-review-runtime:/{exit}' "$compose")
[ -n "$runtime_block" ] || { echo 'github-docs-review-runtime service block is missing' >&2; exit 1; }
webhook_block=$(awk '/^  github-webhook:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-webhook:/{exit}' "$compose")
[ -n "$webhook_block" ] || { echo 'github-webhook service block is missing' >&2; exit 1; }
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

require_runtime() {
  pattern=$1
  printf '%s\n' "$runtime_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-docs-review-runtime" >&2
    exit 1
  }
}

forbid_runtime() {
  pattern=$1
  if printf '%s\n' "$runtime_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in github-docs-review-runtime" >&2
    exit 1
  fi
}

# The runtime owns the durable record, GitHub App projection, candidate bridge,
# and reconciliation. It has no listener; webhook ingress remains separate.
require_runtime 'github_docs_impact_compose_adapter.py.*reconcile.*--loop'
require_runtime 'GC_GITHUB_DOCS_REVIEW_RUNS_DIR: /var/lib/github-intake/docs-review'
require_runtime 'GC_GITHUB_DOCS_CANDIDATE_DIR: /var/lib/github-intake/docs-review/candidates'
require_runtime 'GC_GITHUB_INTAKE_DIRECT_BD: "1"'
require_runtime 'BEADS_DIR:.*CITY_DIR.*\.beads'
require_runtime 'GITHUB_APP_PRIVATE_KEY_PEM:'
require_runtime 'network_mode: "service:city"'
require_runtime 'restart: unless-stopped'
require_runtime 'GC_HOME: /var/lib/gascity'
require_runtime 'state/gc-runtime:/var/lib/gascity'
forbid_runtime '^    ports:'

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
# receives the persisted immutable assignment only after the runtime records it.
require_city 'CODEX_AUTH_FILE.*:/run/secrets/codex-auth.json:ro'
require_city 'GC_CITY_DOCS_REVIEW_ENABLED:.*true'
require_city 'GC_CITY_DOCS_REVIEW_TARGET:.*GC_CITY_DOCS_REVIEW_TARGET'
require_city 'docs-review:/var/lib/github-docs-impact/review'
require_city 'GITHUB_PACK_DIR.*:/opt/gascity-packs:ro'
# City runs as HOST_UID:GID, so its supervisor state and real home must not
# live below /root (which an unprivileged container user cannot traverse).
require_city 'GC_HOME: /var/lib/gascity'
require_city 'HOME: /var/lib/gascity/home'
require_city 'state/gc-runtime:/var/lib/gascity'
forbid_city 'GC_HOME: /root'
forbid_city 'HOME: /root'
forbid_city 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'

if grep -Fq 'github_intake_city_docs_launcher.py' "$root/docker/city-entrypoint.sh"; then
  echo 'retired City docs launcher must be absent' >&2
  exit 1
fi
require '/opt/gascity-packs/github' "$root/docker/city-entrypoint.sh"
require 'gc --city "\$CITY_PATH" import add /opt/gascity-packs/github --name github-docs-impact' "$root/docker/city-entrypoint.sh"
if grep -Fq 'gc --city "$CITY_PATH" --rig "$GC_CITY_DOCS_REVIEW_RIG_DIR" import add /opt/gascity-packs/github' "$root/docker/city-entrypoint.sh"; then
  echo 'github docs-impact is city-scoped and must not be imported into a rig' >&2
  exit 1
fi
require 'github-docs-impact-city-dispatcher' "$root/docker/city-entrypoint.sh"
require '127\.0\.0\.1:8372/api/health' "$root/docker/city-entrypoint.sh"
require 'github_docs_impact_city_dispatcher.py' "$root/Dockerfile.city"
require 'city-docs-impact.toml' "$root/docker/city-entrypoint.sh"
require '^\[providers\.codex-docs-impact\]$' "$root/config/city-docs-impact.toml"
require '^name = "github-docs-impact\.docs-impact-reviewer"$' "$root/config/city-docs-impact.toml"
require '^work_dir = "\.gc/agents/\{\{\.AgentBase\}\}"$' "$root/config/city-docs-impact.toml"
require 'gpt-5\.6-terra' "$root/config/city-docs-impact.toml"
require 'model_reasoning_effort=medium' "$root/config/city-docs-impact.toml"

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
for expected in city github-webhook github-docs-review-runtime; do
  printf '%s\n' "$profile_services" | grep -Fx "$expected" >/dev/null || {
    echo "github-docs-impact profile does not activate $expected" >&2
    exit 1
  }
done

# The authenticated webhook rule builds complete paginated evidence, persists
# the run, and asks the runtime adapter to dispatch only after that record.
require_webhook 'GC_GITHUB_DOCS_REVIEW_RUNS_DIR: /var/lib/github-intake/docs-review'
require_webhook 'GC_GITHUB_DOCS_CANDIDATE_DIR: /var/lib/github-intake/docs-review/candidates'
require_webhook 'GC_SERVICE_STATE_ROOT: /var/lib/github-intake'
require_webhook 'GC_GITHUB_INTAKE_DIRECT_BD: "1"'
require_webhook 'BEADS_DIR:.*CITY_DIR.*\.beads'
require 'github_docs_impact_compose_adapter.py.*intake.*--once' "$rules"
require_webhook 'network_mode: "service:city"'
require_webhook 'GC_HOME: /var/lib/gascity'
require_webhook 'state/gc-runtime:/var/lib/gascity'
require_webhook 'github_docs_impact_webhook.py'
