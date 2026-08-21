#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file; copy .env.example to .env and set role tokens" >&2
  exit 1
fi

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

reviewer_token="$(value_for GITEA_REVIEWER_TOKEN)"
worker_token="$(value_for GITEA_WORKER_TOKEN)"

if [ -z "$reviewer_token" ] || [ -z "$worker_token" ]; then
  printf '%s\n' 'ERROR: set non-empty GITEA_REVIEWER_TOKEN and GITEA_WORKER_TOKEN in .env' >&2
  exit 1
fi
if [ "$reviewer_token" = "$worker_token" ]; then
  printf '%s\n' 'ERROR: reviewer and worker must use distinct Gitea service-account tokens' >&2
  exit 1
fi

printf '%s\n' 'PASS: distinct Gitea MCP role tokens are configured'
