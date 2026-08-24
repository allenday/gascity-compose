#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

value_for() {
  key="$1"
  fallback="$2"
  value="$(awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file")"
  printf '%s' "${value:-$fallback}"
}

woodpecker_port="$(value_for WOODPECKER_PORT 8000)"
curl --fail --silent --show-error "http://127.0.0.1:${woodpecker_port}/api/health" >/dev/null
docker compose --env-file "$env_file" --profile woodpecker ps --status running | grep -q woodpecker-server
docker compose --env-file "$env_file" --profile woodpecker ps --status running | grep -q woodpecker-agent
printf '%s\n' 'PASS: Woodpecker server and agent are healthy; activate the private fixture repo in the local UI to run its pipeline'
