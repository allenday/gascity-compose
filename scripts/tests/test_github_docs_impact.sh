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
require 'github-webhook:8080' "$root/nginx/nginx.conf"
require 'github-admin:8081' "$root/nginx/nginx.conf"
require 'location = /v0/github/webhook' "$root/nginx/nginx.conf"
require 'location = /v0/github/admin' "$root/nginx/nginx.conf"
require 'location \^~ /v0/github/app/' "$root/nginx/nginx.conf"
require 'action = "opened"' "$rules"
require 'action = "reopened"' "$rules"
require 'action = "synchronize"' "$rules"
require 'action = "ready_for_review"' "$rules"
require 'github_intake_docs_impact.py' "$rules"
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
require_worker 'GC_TECHDOCS_SNAPSHOT_FILE: /work/snapshot/snapshot.json'
require_worker 'GC_TECHDOCS_ARTIFACT_FILE: /work/artifact/artifact.json'
require_worker 'github_intake_docs_patch_worker.py'
require_worker 'test -f "\$\$worker"'
require_worker 'GitHub pack lacks github_intake_docs_patch_worker.py'
require_worker 'docs-patch-snapshots.*:/work/snapshot:ro'
require_worker 'docs-patch-artifacts.*:/work/artifact'
require_worker 'read_only: true'
require_worker 'network_mode: "none"'
require_worker 'cap_drop:'
forbid_worker 'GITHUB_(APP|WEBHOOK|TOKEN|INTAKE).*:'
forbid_worker '\$\{CITY_DIR'
forbid_worker './config/github-intake'
forbid_worker '^    ports:'
