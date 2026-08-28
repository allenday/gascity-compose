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
require 'github-docs-model-egress:' "$compose"
require 'github-docs-techdocs-reviewer:' "$compose"
require 'profiles: \[github-docs-impact\]' "$compose"
require 'GC_SERVICE_HOST: 0.0.0.0' "$compose"
require 'GC_SERVICE_PORT: "8080"' "$compose"
require 'HOME: /var/lib/github-intake/home' "$compose"
require 'GITHUB_WEBHOOK_WAN_IP:-127.0.0.1' "$compose"
require 'GITHUB_WEBHOOK_WAN_PORT:-8088' "$compose"
require 'github-webhook:8080' "$root/nginx/nginx.conf"
require 'github-admin:8081' "$root/nginx/nginx.conf"
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
reviewer_block=$(awk '/^  github-docs-techdocs-reviewer:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-techdocs-reviewer:/{exit}' "$compose")
[ -n "$reviewer_block" ] || { echo 'github-docs-techdocs-reviewer service block is missing' >&2; exit 1; }
egress_block=$(awk '/^  github-docs-model-egress:/{inside=1} inside {print} inside && NR > 1 && /^  [^ ]/ && !/^  github-docs-model-egress:/{exit}' "$compose")
[ -n "$egress_block" ] || { echo 'github-docs-model-egress service block is missing' >&2; exit 1; }

require_webhook() {
  pattern=$1
  printf '%s\n' "$webhook_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-webhook" >&2
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

require_reviewer() {
  pattern=$1
  printf '%s\n' "$reviewer_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-docs-techdocs-reviewer" >&2
    exit 1
  }
}

forbid_reviewer() {
  pattern=$1
  if printf '%s\n' "$reviewer_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in github-docs-techdocs-reviewer" >&2
    exit 1
  fi
}

require_egress() {
  pattern=$1
  printf '%s\n' "$egress_block" | grep -Eq "$pattern" || {
    echo "missing $pattern in github-docs-model-egress" >&2
    exit 1
  }
}

forbid_egress() {
  pattern=$1
  if printf '%s\n' "$egress_block" | grep -Eq "$pattern"; then
    echo "forbidden $pattern in github-docs-model-egress" >&2
    exit 1
  fi
}

# The model reviewer has exactly one network path: an internal network whose
# only egress peer is the credentialless, endpoint-pinned gateway.
require_reviewer 'GC_TECHDOCS_MODEL_ENDPOINT: http://github-docs-model-egress:3128/v1'
require_reviewer '^    networks:'
require_reviewer '^      - github-docs-model$'
forbid_reviewer '^      - default$'
forbid_reviewer 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'
forbid_reviewer '\$\{CITY_DIR'
forbid_reviewer '^    ports:'

require_egress 'github_docs_model_egress_proxy.py'
require_egress 'GC_TECHDOCS_MODEL_UPSTREAM_ENDPOINT:.*GC_TECHDOCS_MODEL_ENDPOINT:-'
require_egress '^    networks:'
require_egress '^      - github-docs-model$'
require_egress '^      - default$'
require_egress 'read_only: true'
require_egress 'cap_drop:'
forbid_egress 'GC_TECHDOCS_MODEL_TOKEN'
forbid_egress 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'
forbid_egress '\$\{CITY_DIR'
forbid_egress '^    ports:'

require '^  github-docs-model:$' "$compose"
require '^    internal: true$' "$compose"

# The trusted rule subprocess shares the exact queue directories, waits longer
# than the worker's default adapter timeout, and alone receives the token used
# by the pipeline's projector/publisher.
require_webhook 'GC_GITHUB_DOCS_PATCH_SNAPSHOT_DIR: /var/lib/github-intake/docs-patch-snapshots'
require_webhook 'GC_GITHUB_DOCS_PATCH_ARTIFACT_DIR: /var/lib/github-intake/docs-patch-artifacts'
require_webhook 'GC_GITHUB_DOCS_REVIEW_WAIT_SECONDS:.*GC_GITHUB_DOCS_REVIEW_WAIT_SECONDS:-305'
