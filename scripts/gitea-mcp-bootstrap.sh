#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

if [ ! -e "$env_file" ]; then
  cp .env.example "$env_file"
  chmod 600 "$env_file"
  printf '%s\n' "Created $env_file from .env.example"
fi

compose() {
  docker compose --env-file "$env_file" "$@"
}

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

set_value() {
  key="$1"
  value="$2"
  tmp="$(mktemp "${env_file}.tmp.XXXXXX")"
  awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$env_file" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
}

create_token_if_missing() {
  username="$1"
  email="$2"
  env_key="$3"
  scopes="$4"

  if [ -n "$(value_for "$env_key")" ]; then
    return
  fi

  password="$(openssl rand -base64 36 | tr -d '\n')"
  # Creating an existing user is harmlessly rejected; token generation below
  # remains idempotent because an existing .env value is preserved.
  compose exec -T gitea gitea admin user create \
    --username "$username" \
    --email "$email" \
    --password "$password" \
    --must-change-password=false \
    --restricted >/dev/null 2>&1 || true

  token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$username" \
    --token-name "gascity-mcp-${username}" \
    --scopes "$scopes" \
    --raw)"
  if [ -z "$token" ]; then
    printf '%s\n' "ERROR: could not generate a token for $username" >&2
    exit 1
  fi
  set_value "$env_key" "$token"
  printf '%s\n' "Configured $env_key for $username"
}

# First establish Gitea itself, then create one restricted, non-admin service
# account per MCP permission class. Tokens remain only in ignored .env.
compose up -d --wait --wait-timeout 90 gitea
create_token_if_missing gascity-mcp-mayor gascity-mcp-mayor@localhost GITEA_MAYOR_TOKEN 'read:user,read:repository,read:issue,write:issue'
create_token_if_missing gascity-mcp-observer gascity-mcp-observer@localhost GITEA_OBSERVER_TOKEN 'read:user,read:repository,read:issue'
create_token_if_missing gascity-mcp-witness gascity-mcp-witness@localhost GITEA_WITNESS_TOKEN 'read:user,read:repository,read:issue,write:issue'

ENV_FILE="$env_file" sh ./scripts/gitea-mcp-preflight.sh
compose --profile gitea-mcp up -d --wait --wait-timeout 90
ENV_FILE="$env_file" sh ./scripts/gitea-mcp-smoke.sh
