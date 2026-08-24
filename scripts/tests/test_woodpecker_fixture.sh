#!/usr/bin/env sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
compose="$root/compose.yaml"
fixture="$root/scripts/woodpecker-fixture-bootstrap.sh"
preflight="$root/scripts/woodpecker-preflight.sh"

require() {
  pattern="$1"
  file="$2"
  if ! grep -Eq "$pattern" "$file"; then
    printf 'missing %s in %s\n' "$pattern" "$file" >&2
    exit 1
  fi
}

# The CI services are opt-in and use pinned images. The Docker backend needs a
# socket, but it must be confined to the agent rather than the server.
require 'woodpecker-server:' "$compose"
require 'profiles: \[woodpecker\]' "$compose"
require 'woodpeckerci/woodpecker-server:v3\.12\.0' "$compose"
require '127\.0\.0\.1:\$\{WOODPECKER_PORT:-8000\}:8000' "$compose"
require 'WOODPECKER_GITEA: "true"' "$compose"
require 'WOODPECKER_GITEA_URL: http://gitea:3000' "$compose"
require 'GITEA__server__PUBLIC_URL_DETECTION: auto' "$compose"
require 'woodpecker-agent:' "$compose"
require 'WOODPECKER_REPO_OWNERS: \$\{WOODPECKER_REPO_OWNERS:-woodpecker-fixture\}' "$compose"
require 'WOODPECKER_BACKEND_DOCKER_NETWORK: gascity-woodpecker' "$compose"
require '^  woodpecker:$' "$compose"
require 'name: gascity-woodpecker' "$compose"
require 'cap_drop:' "$compose"
require 'no-new-privileges:true' "$compose"
if grep -Eq 'privileged: true' "$compose"; then
  printf 'Woodpecker must not enable privileged pipeline plugins\n' >&2
  exit 1
fi
socket_services="$(awk '
  /^  [a-z0-9-]+:$/ { service = substr($1, 1, length($1) - 1) }
  /\/var\/run\/docker\.sock/ { print service }
' "$compose")"
if [ "$socket_services" != "woodpecker-agent" ]; then
  printf 'only the Woodpecker agent may receive the Docker socket\n' >&2
  exit 1
fi

# Compose interpolation is part of the fixture's connectivity contract. The
# browser must never be redirected to Docker DNS, while Gitea webhooks must
# never resolve the loopback address inside the Gitea container.
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
docker compose --env-file "$root/.env.example" --profile woodpecker config >"$rendered"
require 'WOODPECKER_EXPERT_FORGE_OAUTH_HOST: http://127\.0\.0\.1:3002' "$rendered"
require 'WOODPECKER_EXPERT_WEBHOOK_HOST: http://woodpecker-server:8000' "$rendered"
require 'GITEA__security__ALLOWED_HOST_LIST: private,loopback' "$rendered"
require 'WOODPECKER_OPEN: "false"' "$rendered"
require 'WOODPECKER_ADMIN: woodpecker-fixture' "$rendered"
require 'WOODPECKER_ENVIRONMENT: GITEA_FIXTURE_PACKAGE_TOKEN:' "$rendered"
if grep -Eq 'WOODPECKER_DEV_GITEA_OAUTH_URL' "$rendered"; then
  printf 'Woodpecker v3 must not use the removed DEV_GITEA_OAUTH_URL setting\n' >&2
  exit 1
fi

# Fixture setup owns a restricted Gitea automation account and creates a
# repository-owned pipeline. It must not use the bootstrap admin credential.
require 'GITEA_WOODPECKER_TOKEN' "$fixture"
require 'GITEA_WOODPECKER_PASSWORD' "$fixture"
require 'GITEA_WOODPECKER_PACKAGE_TOKEN' "$fixture"
require 'gitea admin user create' "$fixture"
require 'gitea_api=.*api/v1' "$fixture"
require 'user/repos' "$fixture"
require '\.woodpecker\.yml' "$fixture"
require 'api/packages/.*/generic/' "$fixture"
require 'CI_COMMIT_SHA' "$fixture"
if grep -Eq 'STACK_PASSWORD|GITEA_MAYOR_TOKEN|GITEA_BRIDGE_TOKEN' "$fixture"; then
  printf 'fixture must not consume privileged Gitea credentials\n' >&2
  exit 1
fi

require 'WOODPECKER_AGENT_SECRET' "$preflight"
require 'WOODPECKER_GITEA_CLIENT' "$preflight"
require 'WOODPECKER_GITEA_SECRET' "$preflight"
require 'GITEA_WOODPECKER_PACKAGE_TOKEN' "$preflight"
require 'woodpecker-preflight' "$root/Makefile"
require 'gascity-woodpecker' "$root/scripts/woodpecker-smoke.sh"
require 'api/healthz' "$root/scripts/woodpecker-smoke.sh"
require 'woodpecker-acceptance' "$root/Makefile"
require 'hooks/.*/deliveries' "$root/scripts/woodpecker-acceptance.sh"
require 'generic/gascity-compose-fixture' "$root/scripts/woodpecker-acceptance.sh"
require 'WOODPECKER_GITEA_BROWSER_URL=' "$root/.env.example"
require 'GITEA_WOODPECKER_PACKAGE_TOKEN=' "$root/.env.example"

printf '%s\n' 'PASS: Woodpecker fixture structural safeguards are configured'
