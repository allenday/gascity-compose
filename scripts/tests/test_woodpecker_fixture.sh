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
require 'woodpecker-agent:' "$compose"
require 'WOODPECKER_REPO_OWNERS: \$\{WOODPECKER_REPO_OWNERS:-woodpecker-fixture\}' "$compose"
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

# Fixture setup owns a restricted Gitea automation account and creates a
# repository-owned pipeline. It must not use the bootstrap admin credential.
require 'GITEA_WOODPECKER_TOKEN' "$fixture"
require 'GITEA_WOODPECKER_PASSWORD' "$fixture"
require 'gitea admin user create' "$fixture"
require 'gitea_api=.*api/v1' "$fixture"
require 'user/repos' "$fixture"
require '\.woodpecker\.yml' "$fixture"
if grep -Eq 'STACK_PASSWORD|GITEA_MAYOR_TOKEN|GITEA_BRIDGE_TOKEN' "$fixture"; then
  printf 'fixture must not consume privileged Gitea credentials\n' >&2
  exit 1
fi

require 'WOODPECKER_AGENT_SECRET' "$preflight"
require 'WOODPECKER_GITEA_CLIENT' "$preflight"
require 'WOODPECKER_GITEA_SECRET' "$preflight"
require 'woodpecker-preflight' "$root/Makefile"

printf '%s\n' 'PASS: Woodpecker fixture is profile-gated and least-privileged'
