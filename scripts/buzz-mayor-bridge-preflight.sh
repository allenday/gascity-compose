#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
[ -r "$env_file" ] || { printf '%s\n' "ERROR: copy .env.example to $env_file first" >&2; exit 1; }

value_for() {
  awk -F= -v key="$1" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

require_value() {
  value="$(value_for "$1")"
  [ -n "$value" ] && [ "$value" != bootstrap-required ] && ! printf '%s' "$value" | grep -q '^CHANGE_ME' || {
    printf '%s\n' "ERROR: $1 must be set in $env_file" >&2
    exit 1
  }
  printf '%s' "$value"
}

require_hex() {
  value="$(require_value "$1")"
  printf '%s' "$value" | grep -Eq "^[0-9a-f]{$2}$" || {
    printf '%s\n' "ERROR: $1 must be $2 lowercase hexadecimal characters" >&2
    exit 1
  }
}

require_url() {
  value="$(require_value "$1")"
  printf '%s' "$value" | grep -Eq "^$2://[^/@?#[:space:]]+(:[0-9]+)?(/[^?#[:space:]]*)?$" || {
    printf '%s\n' "ERROR: $1 must be a canonical $2 URL without userinfo, query, or fragment" >&2
    exit 1
  }
}

canonical_authority() {
  url="$1"
  scheme="${url%%://*}"
  authority_and_path="${url#*://}"
  authority="${authority_and_path%%/*}"
  case "$authority" in
    *:*) ;;
    *)
      case "$scheme" in
        http|ws) authority="$authority:80" ;;
        https|wss) authority="$authority:443" ;;
      esac
      ;;
  esac
  printf '%s' "$authority" | tr '[:upper:]' '[:lower:]'
}

public_url="$(require_value BUZZ_PUBLIC_RELAY_URL)"
require_url BUZZ_PUBLIC_RELAY_URL 'https?'
human_url="$(require_value BUZZ_RELAY_URL)"
require_url BUZZ_RELAY_URL 'wss?'
[ "$(canonical_authority "$public_url")" = "$(canonical_authority "$human_url")" ] || {
  printf '%s\n' 'ERROR: canonical relay host authorities differ between BUZZ_RELAY_URL and BUZZ_PUBLIC_RELAY_URL' >&2
  exit 1
}
internal_url="$(require_value BUZZ_MAYOR_BRIDGE_RELAY_URL)"
[ "$internal_url" = http://buzz-relay:3000 ] || {
  printf '%s\n' 'ERROR: BUZZ_MAYOR_BRIDGE_RELAY_URL must be http://buzz-relay:3000' >&2
  exit 1
}
require_hex BUZZ_BRIDGE_PRIVATE_KEY 64
require_hex BUZZ_BRIDGE_PUBLIC_KEY 64
require_hex BUZZ_CHANNEL_ADMIN_PRIVATE_KEY 64
channel="$(require_value BUZZ_MAYOR_CHANNEL_ID)"
printf '%s' "$channel" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || {
  printf '%s\n' 'ERROR: BUZZ_MAYOR_CHANNEL_ID must be a fixed channel UUID' >&2
  exit 1
}

humans="$(require_value BUZZ_ALLOWED_HUMAN_PUBKEYS)"
seen=''
for key in $(printf '%s' "$humans" | tr ',' ' '); do
  printf '%s' "$key" | grep -Eq '^[0-9a-f]{64}$' || {
    printf '%s\n' 'ERROR: BUZZ_ALLOWED_HUMAN_PUBKEYS must contain 64-character lowercase hex keys' >&2
    exit 1
  }
  case ",$seen," in *",$key,"*) printf '%s\n' "ERROR: duplicate Buzz human public key $key" >&2; exit 1 ;; esac
  seen="${seen:+$seen,}$key"
done

for key in BUZZ_POLL_INTERVAL_SECONDS BUZZ_BATCH_SIZE BUZZ_OVERLAP_SECONDS BUZZ_READY_MAX_AGE_SECONDS; do
  value="$(require_value "$key")"
  printf '%s' "$value" | grep -Eq '^[1-9][0-9]*$' || { printf '%s\n' "ERROR: $key must be positive" >&2; exit 1; }
done
for key in MCP_AGENT_MAIL_BEARER_TOKEN MCP_AGENT_MAIL_PROJECT_KEY BUZZ_AGENT_MAIL_BRIDGE_IDENTITY BUZZ_AGENT_MAIL_MAYOR_IDENTITY BUZZ_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN; do
  require_value "$key" >/dev/null
done
[ "$(value_for BUZZ_AGENT_MAIL_BRIDGE_IDENTITY)" != "$(value_for BUZZ_AGENT_MAIL_MAYOR_IDENTITY)" ] || {
  printf '%s\n' 'ERROR: Buzz bridge and Mayor Agent Mail identities must differ' >&2
  exit 1
}

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
docker compose --env-file "$env_file" --profile buzz --profile buzz-mayor-bridge config >"$rendered"
relay_image="$(awk '
  $0 == "  buzz-relay:" { inside = 1; next }
  inside && /^  [[:alnum:]_-]+:$/ { exit }
  inside && /^    image: / { sub(/^    image: /, ""); print; exit }
' "$rendered")"
printf '%s\n' "$relay_image" | grep -Eq '^ghcr\.io/block/buzz:sha-[0-9a-f]{7,}@sha256:[0-9a-f]{64}$' || {
  printf '%s\n' 'ERROR: floating Buzz relay image is not allowed' >&2
  exit 1
}
bridge_image="$(awk '
  $0 == "  buzz-mayor-bridge:" { inside = 1; next }
  inside && /^  [[:alnum:]_-]+:$/ { exit }
  inside && /^    image: / { sub(/^    image: /, ""); print; exit }
' "$rendered")"
printf '%s\n' "$bridge_image" | grep -Eq '^ghcr\.io/cyberstorm-dev/gascity-buzz-mayor-bridge@sha256:[0-9a-f]{64}$' || {
  printf '%s\n' 'ERROR: floating Buzz Mayor bridge image is not allowed' >&2
  exit 1
}

# Pull the exact pinned Buzz-owned image, then execute its bounded signed query
# before steady-state startup. The client sends the request to the internal relay while
# deriving the canonical public Host only from BUZZ_PUBLIC_RELAY_URL.
docker compose --env-file "$env_file" --profile buzz --profile buzz-mayor-bridge \
  up -d --wait --wait-timeout 120 buzz-relay
docker compose --env-file "$env_file" --profile buzz --profile buzz-mayor-bridge \
  run --rm --no-deps buzz-mayor-bridge --preflight
docker compose --env-file "$env_file" --profile buzz --profile buzz-mayor-bridge \
  up -d --wait --wait-timeout 180 mcp-agent-mail buzz-mayor-bridge
docker compose --env-file "$env_file" --profile buzz --profile buzz-mayor-bridge \
  exec -T buzz-mayor-bridge /busybox wget -q -O - http://127.0.0.1:8080/readyz >/dev/null
printf '%s\n' 'PASS: authenticated internal Buzz query preserved the canonical public relay Host'
