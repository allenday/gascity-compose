#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT

docker compose --project-directory "$root" --env-file "$root/.env.example" \
  --profile buzz --profile buzz-mayor-bridge config >"$rendered"

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

service_block() {
  awk -v service="$1" '
    $0 == "  " service ":" { inside = 1; next }
    inside && /^  [[:alnum:]_-]+:$/ { exit }
    inside { print }
  ' "$rendered"
}

environment_block() {
  printf '%s\n' "$1" | awk '
    /^    environment:$/ { inside = 1; next }
    inside && /^    [[:lower:]][[:alnum:]_-]*:$/ { exit }
    inside { print }
  '
}

require() {
  grep -Eq "$1" "$2" || fail "missing required Buzz Mayor bridge contract: $1"
}

bridge=$(service_block buzz-mayor-bridge)
[ -n "$bridge" ] || fail 'missing buzz-mayor-bridge service'
printf '%s\n' "$bridge" | grep -Fq 'image: ghcr.io/cyberstorm-dev/gascity-buzz-mayor-bridge@sha256:13fda15e66733adf199e3b5d7ea842a2c3397f3a9f40cc97c6c826db49fcc20f' ||
  fail 'buzz-mayor-bridge must deploy the pinned Buzz-owned image'
if printf '%s\n' "$bridge" | grep -Eq '^    build:$'; then
  fail 'buzz-mayor-bridge must not build from a local source checkout'
fi
if printf '%s\n' "$bridge" | grep -Eq '^    ports:$'; then
  fail 'buzz-mayor-bridge must not publish a host port'
fi
printf '%s\n' "$bridge" | grep -Eq '^    user: "?65532:65532"?$' ||
  fail 'buzz-mayor-bridge must run as the non-root distroless identity'
printf '%s\n' "$bridge" | grep -Fq '/var/lib/buzz-mayor-bridge/ledger.json' ||
  fail 'buzz-mayor-bridge must use its dedicated durable ledger'
printf '%s\n' "$bridge" | grep -Eq 'source: .*/state/buzz-mayor-bridge' ||
  fail 'buzz-mayor-bridge must mount only its dedicated ledger state'
printf '%s\n' "$bridge" | grep -Fq '/readyz' ||
  fail 'buzz-mayor-bridge must provide a readiness healthcheck'
printf '%s\n' "$bridge" | grep -Eq '^      buzz-relay:$' ||
  fail 'buzz-mayor-bridge must depend on buzz-relay'
printf '%s\n' "$bridge" | grep -Eq '^        condition: service_healthy$' ||
  fail 'buzz-mayor-bridge dependencies must be health-gated'
printf '%s\n' "$bridge" | grep -Fq 'BUZZ_AGENT_MAIL_URL: http://mcp-agent-mail:8765/mcp' ||
  fail 'buzz-mayor-bridge must use only the private Agent Mail endpoint'
printf '%s\n' "$bridge" | grep -Fq 'BUZZ_RELAY_URL: http://buzz-relay:3000' ||
  fail 'buzz-mayor-bridge must use the private relay transport coordinate'
printf '%s\n' "$bridge" | grep -Fq 'BUZZ_PUBLIC_RELAY_URL: http://100.64.0.1:3003' ||
  fail 'buzz-mayor-bridge must preserve the canonical public relay authority'

bridge_environment=$(environment_block "$bridge")
for forbidden in 'GITEA_' 'GASCITY_' 'CITY_' 'MAYOR_PRIVATE' '/run/secrets/city-mail' '/var/lib/gascity' '/run/codex'; do
  if printf '%s\n' "$bridge_environment" | grep -Fq "$forbidden"; then
    fail "buzz-mayor-bridge must not receive $forbidden authority"
  fi
done

buzz_example=$(awk '
  /^# Isolated private Buzz Mayor bridge\./ { capture = 1 }
  capture && /^# Optional single-binding/ { exit }
  capture { print }
' "$root/.env.example")
for forbidden in 'GASCITY_GITEA_DIR' 'GITEA_BRIDGE_DIR' 'GASCITY_GITEA_REF'; do
  if printf '%s\n' "$buzz_example" | grep -Fq "$forbidden"; then
    fail "Buzz Mayor bridge example configuration must not mention $forbidden"
  fi
done

agent_mail=$(service_block mcp-agent-mail)
[ -n "$agent_mail" ] || fail 'missing mcp-agent-mail service'
printf '%s\n' "$agent_mail" | grep -Fq 'buzz-mayor-bridge' ||
  fail 'mcp-agent-mail must be enabled by the buzz-mayor-bridge profile'

require '^  buzz-mayor-bridge:$' "$root/compose.yaml"
require 'profiles: \[buzz-mayor-bridge\]' "$root/compose.yaml"
require 'image: ghcr\.io/cyberstorm-dev/gascity-buzz-mayor-bridge@sha256:13fda15e66733adf199e3b5d7ea842a2c3397f3a9f40cc97c6c826db49fcc20f' "$root/compose.yaml"
require 'BUZZ_PUBLIC_RELAY_URL' "$root/compose.yaml"
require 'BUZZ_RELAY_URL: \$\{BUZZ_MAYOR_BRIDGE_RELAY_URL:\?Set BUZZ_MAYOR_BRIDGE_RELAY_URL in \.env\}' "$root/compose.yaml"

for script in buzz-mayor-bridge-bootstrap.sh buzz-mayor-bridge-preflight.sh; do
  [ -f "$root/scripts/$script" ] || fail "missing $script"
  sh -n "$root/scripts/$script"
done
for forbidden in 'GASCITY_GITEA_DIR' 'GITEA_BRIDGE_DIR' 'GASCITY_GITEA_REF'; do
  if grep -Fq "$forbidden" "$root/scripts/buzz-mayor-bridge-preflight.sh"; then
    fail "Buzz Mayor bridge preflight must not use $forbidden"
  fi
done
require 'buzz-admin add-member' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'cli_relay_url=' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'BUZZ_RELAY_URL="\$cli_relay_url"' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'channels create' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'channels add-member' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'compose --profile buzz --profile buzz-mayor-bridge up -d --wait --wait-timeout 90 mcp-agent-mail' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'compose --profile buzz --profile buzz-mayor-bridge exec -T mcp-agent-mail' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'buzz-mayor-bridge --preflight' "$root/scripts/buzz-mayor-bridge-preflight.sh"
require '/readyz' "$root/scripts/buzz-mayor-bridge-preflight.sh"
require 'canonical relay host authorities differ' "$root/scripts/buzz-mayor-bridge-preflight.sh"
require 'floating Buzz relay image' "$root/scripts/buzz-mayor-bridge-preflight.sh"

require '^buzz-mayor-bridge-bootstrap:' "$root/Makefile"
require '^buzz-mayor-bridge-up:' "$root/Makefile"
require '^buzz-mayor-bridge-smoke:' "$root/Makefile"
require 'test_buzz_mayor_bridge\.sh' "$root/Makefile"
require 'buzz-mayor-bridge' "$root/.github/workflows/ci.yml"
require 'test_buzz_mayor_bridge\.sh' "$root/.github/workflows/ci.yml"
require 'BUZZ_MAYOR_BRIDGE_RELAY_URL: http://buzz-relay:3000' "$root/.github/workflows/ci.yml"
require 'BUZZ_PUBLIC_RELAY_URL: http://100.64.0.1:3003' "$root/.github/workflows/ci.yml"

printf '%s\n' 'PASS: isolated Buzz Mayor bridge deployment contract is configured'
