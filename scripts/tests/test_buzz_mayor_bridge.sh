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
printf '%s\n' "$bridge" | grep -Fq 'BUZZ_PUBLIC_RELAY_URL: ws://100.64.0.1:3003' ||
  fail 'buzz-mayor-bridge must preserve the canonical public relay authority'

bridge_environment=$(environment_block "$bridge")
for forbidden in 'GITEA_' 'GASCITY_' 'CITY_' 'MAYOR_PRIVATE' '/run/secrets/city-mail' '/var/lib/gascity' '/run/codex'; do
  if printf '%s\n' "$bridge_environment" | grep -Fq "$forbidden"; then
    fail "buzz-mayor-bridge must not receive $forbidden authority"
  fi
done

agent_mail=$(service_block mcp-agent-mail)
[ -n "$agent_mail" ] || fail 'missing mcp-agent-mail service'
printf '%s\n' "$agent_mail" | grep -Fq 'buzz-mayor-bridge' ||
  fail 'mcp-agent-mail must be enabled by the buzz-mayor-bridge profile'

require '^  buzz-mayor-bridge:$' "$root/compose.yaml"
require 'profiles: \[buzz-mayor-bridge\]' "$root/compose.yaml"
require 'GASCITY_GITEA_REF: \$\{GASCITY_GITEA_REF:\?Set GASCITY_GITEA_REF in \.env\}' "$root/compose.yaml"
require 'git status --porcelain --untracked-files=all' "$root/compose.yaml"
require 'git rev-parse HEAD' "$root/compose.yaml"
require 'go build .*\./cmd/buzz-mayor-bridge' "$root/compose.yaml"
require 'BUZZ_PUBLIC_RELAY_URL' "$root/compose.yaml"

for script in buzz-mayor-bridge-bootstrap.sh buzz-mayor-bridge-preflight.sh; do
  [ -f "$root/scripts/$script" ] || fail "missing $script"
  sh -n "$root/scripts/$script"
done
require 'GASCITY_GITEA_REF' "$root/scripts/buzz-mayor-bridge-preflight.sh"
require 'buzz-admin add-member' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'channels create' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require 'channels add-member' "$root/scripts/buzz-mayor-bridge-bootstrap.sh"
require '/readyz' "$root/scripts/buzz-mayor-bridge-preflight.sh"

require '^buzz-mayor-bridge-bootstrap:' "$root/Makefile"
require '^buzz-mayor-bridge-up:' "$root/Makefile"
require '^buzz-mayor-bridge-smoke:' "$root/Makefile"
require 'test_buzz_mayor_bridge\.sh' "$root/Makefile"
require 'buzz-mayor-bridge' "$root/.github/workflows/ci.yml"
require 'test_buzz_mayor_bridge\.sh' "$root/.github/workflows/ci.yml"

printf '%s\n' 'PASS: isolated Buzz Mayor bridge deployment contract is configured'
