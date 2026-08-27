#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT

docker compose --project-directory "$root" --env-file "$root/.env.example" \
  --profile buzz config >"$rendered"

require() {
  if ! grep -Eq "$1" "$2"; then
    printf '%s\n' "missing required Buzz relay contract: $1" >&2
    exit 1
  fi
}

service_block() {
  awk -v service="$1" '
    $0 == "  " service ":" { inside = 1; next }
    inside && /^  [[:alnum:]_-]+:$/ { exit }
    inside { print }
  ' "$rendered"
}

relay=$(service_block buzz-relay)
postgres=$(service_block buzz-postgres)
redis=$(service_block buzz-redis)
object_store=$(service_block buzz-minio)

[ -n "$relay" ] || { printf '%s\n' 'missing buzz-relay service' >&2; exit 1; }
if printf '%s\n' "$relay" | grep -Eq '^    ports:$'; then
  printf '%s\n' 'buzz-relay must not publish a host port' >&2
  exit 1
fi
printf '%s\n' "$relay" | grep -Eq 'ghcr\.io/block/buzz:sha-[0-9a-f]{7,}@sha256:[0-9a-f]{64}' || {
  printf '%s\n' 'buzz-relay image must use a published sha tag and immutable digest' >&2
  exit 1
}
printf '%s\n' "$relay" | grep -Eq 'BUZZ_REQUIRE_MEDIA_GET_AUTH: "?true"?' || {
  printf '%s\n' 'buzz-relay must require media GET authentication' >&2
  exit 1
}
require 'source: .*/state/buzz-git' "$rendered"
require 'source: .*/state/buzz-postgres' "$rendered"
require 'source: .*/state/buzz-redis' "$rendered"
require 'source: .*/state/buzz-minio' "$rendered"
printf '%s\n' "$relay" | grep -Fq '/_readiness' || {
  printf '%s\n' 'buzz-relay readiness healthcheck is required' >&2
  exit 1
}
printf '%s\n' "$relay" | grep -Fq '/_liveness' || {
  printf '%s\n' 'buzz-relay liveness healthcheck is required' >&2
  exit 1
}
[ -n "$postgres" ] && [ -n "$redis" ] && [ -n "$object_store" ] || {
  printf '%s\n' 'Buzz durable dependency services are required' >&2
  exit 1
}

require '^  buzz-relay:$' "$root/compose.yaml"
for service in buzz-relay buzz-postgres buzz-redis buzz-minio buzz-minio-init; do
  if ! awk -v service="$service" '
    $0 == "  " service ":" { inside = 1; next }
    inside && /^  [[:alnum:]_-]+:$/ { exit }
    inside && $0 ~ /profiles: \[buzz\]/ { found = 1 }
    END { exit !found }
  ' "$root/compose.yaml"; then
    printf '%s\n' "$service must be gated by the buzz profile" >&2
    exit 1
  fi
done
require '\$\{TAILNET_IP:\?Set TAILNET_IP in \.env\}:\$\{BUZZ_PORT:-3003\}:3003' "$root/compose.yaml"
require '3003  buzz-relay:3000;' "$root/nginx/nginx.conf"
require 'listen 3003;' "$root/nginx/nginx.conf"
require 'proxy_set_header Upgrade \$http_upgrade;' "$root/nginx/nginx.conf"
require 'BUZZ_RELAY_URL=' "$root/.env.example"
require 'BUZZ_MEDIA_BASE_URL=' "$root/.env.example"
require 'BUZZ_CORS_ORIGINS=' "$root/.env.example"
require 'BUZZ_PORT=3003' "$root/.env.example"

printf '%s\n' 'PASS: pinned Tailnet-only Buzz relay profile is configured'
