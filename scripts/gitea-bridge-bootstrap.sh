#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

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

compose up -d --wait --wait-timeout 90 gitea

if [ -z "$(value_for GITEA_BRIDGE_TOKEN)" ]; then
  password="$(openssl rand -base64 36 | tr -d '\n')"
  compose exec -T gitea gitea admin user create \
    --username gascity-gitea-bridge \
    --email gascity-gitea-bridge@localhost \
    --password "$password" \
    --must-change-password=false \
    --restricted >/dev/null 2>&1 || true
  token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username gascity-gitea-bridge \
    --token-name gascity-gitea-bridge \
    --scopes 'read:user,read:repository,read:issue,write:issue' \
    --raw)"
  set_value GITEA_BRIDGE_TOKEN "$token"
  printf '%s\n' 'Configured GITEA_BRIDGE_TOKEN for restricted gascity-gitea-bridge account'
fi

for key in GITEA_BRIDGE_ISSUE_URL GASCITY_BRIDGE_RUN_ID; do
  if ! grep -q "^${key}=" "$env_file"; then
    set_value "$key" ""
  fi
done

if [ -z "$(value_for GITEA_BRIDGE_ISSUE_URL)" ] || [ -z "$(value_for GASCITY_BRIDGE_RUN_ID)" ]; then
  printf '%s\n' 'PENDING: set GITEA_BRIDGE_ISSUE_URL and GASCITY_BRIDGE_RUN_ID before starting the bridge'
  exit 0
fi

printf '%s\n' 'PASS: Gitea bridge credential and binding are configured'
