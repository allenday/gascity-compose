#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file; copy .env.example to .env first" >&2
  exit 1
fi

require_value() {
  key="$1"
  value="$(awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file")"
  if [ -z "$value" ]; then
    printf '%s\n' "ERROR: set $key in $env_file" >&2
    exit 1
  fi
}

require_value WOODPECKER_AGENT_SECRET
require_value WOODPECKER_GITEA_CLIENT
require_value WOODPECKER_GITEA_SECRET
require_value GITEA_WOODPECKER_PACKAGE_TOKEN

printf '%s\n' 'PASS: Woodpecker credentials are present'
