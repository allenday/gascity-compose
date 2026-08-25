#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file; copy .env.example to .env first" >&2
  exit 1
fi

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

username="$(value_for STACK_USERNAME)"
password="$(value_for STACK_PASSWORD)"
if [ -z "$username" ] || [ -z "$password" ]; then
  printf '%s\n' 'ERROR: set STACK_USERNAME and STACK_PASSWORD in the environment file' >&2
  exit 1
fi

# Bind mounts are created by the host as root. Match each image's declared
# runtime UID before its first start; repeated chowns are harmless.
mkdir -p state/gitea/data state/gitea/config state/grafana state/loki state/prometheus state/mcp-agent-mail nginx
chown -R 1000:1000 state/gitea
chown -R 472:472 state/grafana
chown -R 10001:10001 state/loki
chown -R 65534:65534 state/prometheus
chown -R 999:999 state/mcp-agent-mail

compose() {
  docker compose --env-file "$env_file" "$@"
}

# Establish Gitea after fixing ownership. Creation handles a fresh state;
# change-password makes subsequent runs converge on the staging credential.
compose up -d --wait --wait-timeout 90 gitea
compose exec -T gitea gitea admin user create \
  --username "$username" \
  --password "$password" \
  --email "${username}@localhost" \
  --admin \
  --must-change-password=false >/dev/null 2>&1 || true
compose exec -T gitea gitea admin user change-password \
  --username "$username" \
  --password "$password" \
  --must-change-password=false >/dev/null

printf '%s\n' 'PASS: state ownership and Gitea admin are bootstrapped'
