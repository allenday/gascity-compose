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

role_keys='GITEA_MAYOR_TOKEN GITEA_OBSERVER_TOKEN GITEA_WITNESS_TOKEN'
tokens=''
for key in $role_keys; do
  value="$(value_for "$key")"
  if [ -z "$value" ]; then
    printf '%s\n' "ERROR: set non-empty $key in .env" >&2
    exit 1
  fi
  tokens="${tokens}${value}\n"
done

duplicates="$(printf '%b' "$tokens" | sort | uniq -d)"
if [ -n "$duplicates" ]; then
  printf '%s\n' 'ERROR: every Gitea MCP permission class must use a distinct service-account token' >&2
  exit 1
fi

printf '%s\n' 'PASS: three distinct Gitea MCP permission-class tokens are configured'
