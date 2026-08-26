#!/usr/bin/env sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
rendered="$(mktemp)"
bridge="$(mktemp)"
mail="$(mktemp)"
city="$(mktemp)"
launcher="$(mktemp)"
trap 'rm -f "$rendered" "$bridge" "$mail" "$city" "$launcher"' EXIT

# The bridge profile is operationally self-contained: callers must not need to
# know or enable Agent Mail's implementation profile separately.
(
  cd "$root"
  docker compose --env-file .env.example --profile gitea-mail-bridge config -q
)

docker compose --project-directory "$root" --env-file "$root/.env.example" \
  --profile city --profile mcp --profile gitea-mail-bridge config >"$rendered"

service_block() {
  service="$1"
  destination="$2"
  awk -v service="$service" '
    $0 == "  " service ":" { capture = 1 }
    capture && /^  [a-zA-Z0-9_-]+:$/ && $0 != "  " service ":" { exit }
    capture { print }
  ' "$rendered" >"$destination"
}

require() {
  pattern="$1"
  file="$2"
  if ! grep -Eq -- "$pattern" "$file"; then
    printf 'missing %s in %s\n' "$pattern" "$file" >&2
    exit 1
  fi
}

service_block gitea-mail-bridge "$bridge"
service_block mcp-agent-mail "$mail"
service_block city "$city"
service_block city-mail-launcher "$launcher"

# The intake bridge is a private, independently stateful process with only
# read-side Gitea and authenticated Agent Mail capabilities.
require '^  gitea-mail-bridge:$' "$bridge"
require 'user: 65532:65532' "$bridge"
require 'source: .*/state/gitea-mail-bridge' "$bridge"
require 'target: /var/lib/gitea-mail-bridge' "$bridge"
require 'INTAKE_LEDGER_PATH: /var/lib/gitea-mail-bridge/ledger.json' "$bridge"
require 'GITEA_URL: http://gitea:3000' "$bridge"
require 'INTAKE_MAIL_URL: http://mcp-agent-mail:8765/mcp' "$bridge"
require 'gitea:' "$bridge"
require 'condition: service_healthy' "$bridge"
require 'mcp-agent-mail:' "$bridge"
if grep -Eq '^    ports:' "$bridge"; then
  printf '%s\n' 'gitea-mail-bridge must not publish a host port' >&2
  exit 1
fi
if grep -Eq 'GASCITY_API_URL|GASCITY_RUN_ID|GITEA_ISSUE_URL' "$bridge"; then
  printf '%s\n' 'gitea-mail-bridge must not receive City mutation or status-bridge bindings' >&2
  exit 1
fi

# Remote Mail calls fail closed without a bearer and use registration tokens
# for the three distinct identities. Local unauthenticated bypass is disabled.
require 'HTTP_BEARER_TOKEN:' "$mail"
require 'HTTP_RBAC_DEFAULT_ROLE: writer' "$mail"
require 'HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED: "false"' "$mail"
require 'test -n "\$\$HTTP_BEARER_TOKEN"' "$mail"
require 'test "\$\$HTTP_BEARER_TOKEN" != bootstrap-required' "$mail"
require 'NOTIFICATIONS_ENABLED: "true"' "$mail"
require 'NOTIFICATIONS_SIGNALS_DIR: /var/lib/mcp-mail/signals' "$mail"
require 'MCP_AGENT_MAIL_PROJECT_KEY:' "$city"
require 'CITY_MAIL_SIGNAL_FILE:' "$city"
if grep -Eq 'MCP_AGENT_MAIL_BEARER_TOKEN:|MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN:' "$city"; then
  printf '%s\n' 'Mayor Mail secrets must not enter the City supervisor ambient environment' >&2
  exit 1
fi

# The launcher is a private, least-privilege consumer of start authorizations.
require '^  city-mail-launcher:$' "$launcher"
require 'target: /run/secrets/city-mail/launcher.env' "$launcher"
require 'read_only: true' "$launcher"
require 'target: /var/lib/city-mail-launcher' "$launcher"
if [ "$(grep -Fc 'GASCITY_SOURCE_DIR' "$root/compose.yaml")" -ne 1 ]; then
  printf '%s\n' 'Mail-facing launcher must not mount the Gas City source' >&2
  exit 1
fi
if grep -Eq '^    ports:|GITEA_|GASCITY_API_URL|MCP_AGENT_MAIL_MAYOR|CODEX_AUTH_FILE' "$launcher"; then
  printf '%s\n' 'City launcher must not receive public, Gitea, Mayor, or City API authority' >&2
  exit 1
fi

# Gitea may deliver only to the internal bridge hostname, and the source image
# pin must be the immutable Task 4 merge.
require 'GITEA__webhook__ALLOWED_HOST_LIST: 127.0.0.1,localhost,woodpecker-server,gitea-mail-bridge' "$rendered"
require 'GITEA__security__ALLOWED_HOST_LIST: loopback,woodpecker-server,gitea-mail-bridge' "$rendered"
require 'GASCITY_GITEA_REF: 6a2c96f51f51464d7820756d8becae044cdad3eb' "$rendered"
require 'git status --porcelain --untracked-files=all' "$rendered"

# Mayor gets Agent Mail through its role-specific Codex profile. The shared
# Codex configuration deliberately contains no Agent Mail endpoint.
require '^\[mcp_servers\.agent_mail\]$' "$root/codex/mayor.config.toml.template"
require 'url = "http://127.0.0.1:8767/mcp"' "$root/codex/mayor.config.toml.template"
if grep -Eq 'bearer_token_env_var|REGISTRATION_TOKEN' "$root/codex/mayor.config.toml.template"; then
  printf '%s\n' 'Codex must not receive Agent Mail credentials; the Mayor-only proxy injects them' >&2
  exit 1
fi
require '^\[mcp_servers\.gitea\]$' "$root/codex/mayor.config.toml.template"
require 'url = "http://gitea-mcp-mayor:8080/mcp"' "$root/codex/mayor.config.toml.template"
require '^model = "gpt-5\.4"$' "$root/codex/mayor.config.toml.template"
if grep -Eq '^model_provider\s*=' "$root/codex/mayor.config.toml.template"; then
  printf '%s\n' 'Mayor Codex profile must use the authenticated default provider' >&2
  exit 1
fi
if grep -Eq '^\[mcp_servers\.agent_mail\]$' "$root/codex/config.toml.template"; then
  printf '%s\n' 'Agent Mail MCP must not be configured in the shared Codex profile' >&2
  exit 1
fi
require 'command = "/usr/local/bin/codex-mayor"' "$root/config/city-cost-safe.toml"
require 'resume_command = "/usr/local/bin/codex-mayor resume \{\{.SessionKey\}\}"' "$root/config/city-cost-safe.toml"
require 'args_append = \["--profile", "mayor"\]' "$root/config/city-cost-safe.toml"
require '^\[providers\.codex-mayor-runpod\]$' "$root/config/city-cost-safe.toml"
require '^provider = "codex-mayor-runpod"$' "$root/config/city-cost-safe.toml"
if grep -Eq 'MCP_AGENT_MAIL_(BEARER|MAYOR_REGISTRATION)_TOKEN' "$root/config/city-cost-safe.toml"; then
  printf '%s\n' 'static Gas City config must not expand Mayor secrets from supervisor env' >&2
  exit 1
fi

for script in gitea-mail-bridge-bootstrap.sh gitea-mail-bridge-smoke.sh gitea-mail-launcher-smoke.sh gitea-mail-acceptance-demo.sh city-mail-wake.sh codex-mayor; do
  sh -n "$root/scripts/$script"
done
python3 -c 'import pathlib,sys; path=pathlib.Path(sys.argv[1]); compile(path.read_text(), str(path), "exec")' "$root/scripts/city-mail-mcp-proxy.py"
require 'MCP_AGENT_MAIL_PROJECT_PATH' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'chown -R 65532:65532 state/gitea-mail-bridge' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'launcher_uid="\$\(value_for HOST_UID\)"' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'launcher_gid="\$\(value_for HOST_GID\)"' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'chown -R "\$launcher_uid:\$launcher_gid" state/city-mail-launcher' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
bridge_stop_line="$(grep -n 'stop gitea-mail-bridge' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
ledger_chown_line="$(grep -n 'chown -R 65532:65532 state/gitea-mail-bridge' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
if [ -z "$bridge_stop_line" ] || [ "$bridge_stop_line" -ge "$ledger_chown_line" ]; then
  printf '%s\n' 'bootstrap must stop the prior bridge before migrating ledger ownership' >&2
  exit 1
fi
require 'GITEA_MAIL_BRIDGE_ADMIN_TOKEN' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'GITEA_MAIL_BRIDGE_ADMIN_TOKEN_VERSION' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'GITEA_MAIL_BRIDGE_ADMIN_TOKEN' "$root/scripts/gitea-mail-bridge-smoke.sh"
for script in gitea-mail-bridge-bootstrap.sh gitea-mail-bridge-smoke.sh gitea-mail-launcher-smoke.sh; do
  require 'gitea_api="http://gitea:3000/api/v1"|api="http://gitea:3000/api/v1"' "$root/scripts/$script"
  require 'command curl --connect-to "gitea:3000:127\.0\.0\.1:\$\{gitea_port\}"' "$root/scripts/$script"
done
require 'url "\$instance/\$repository/issues/\$issue_number"' "$root/scripts/gitea-mail-bridge-smoke.sh"
require 'while \[ "\$attempts" -lt 90 \]; do' "$root/scripts/gitea-mail-bridge-smoke.sh"
if [ "$(grep -Fc 'while [ "$attempts" -lt 90 ]; do' "$root/scripts/gitea-mail-bridge-smoke.sh")" -ne 3 ]; then
  printf '%s\n' 'City mail smoke waits must cover webhook settlement, authorization, and binding recovery' >&2
  exit 1
fi
require 'gitea admin user list' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require '--must-change-password=false' "$root/scripts/bootstrap.sh"
require 'city-mail-secrets/mayor.env' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'city-mail-secrets/launcher.env' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'city-mail-wake' "$root/docker/city-entrypoint.sh"
require 'city-mail-mcp-proxy' "$root/scripts/codex-mayor"
require '/usr/local/bin/codex "\$@" </dev/tty &' "$root/scripts/codex-mayor"
require 'COPY scripts/city-mail-mcp-proxy.py /usr/local/bin/city-mail-mcp-proxy' "$root/Dockerfile.city"
require 'bash ca-certificates curl git jq python3 tini' "$root/Dockerfile.city"
require 'timeout --signal=TERM' "$root/scripts/city-mail-wake.sh"
require 'collaborators/\$bridge_login' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'permission":"read' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'collaborators/\$intake_account' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'permission":"write' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require '^gitea-mail-bridge-bootstrap:' "$root/Makefile"
require '^gitea-mail-bridge-up:' "$root/Makefile"
require '^gitea-mail-bridge-smoke:' "$root/Makefile"
require '^gitea-mail-launcher-up:' "$root/Makefile"
require '^gitea-mail-launcher-smoke:' "$root/Makefile"
require '^gitea-mail-acceptance-demo:' "$root/Makefile"
require 'CITY_MAIL_LAUNCHER_RIG: my-project' "$root/.github/workflows/ci.yml"
require 'PASS: real City launcher fixture issue #' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'gitea-mail-launcher-up' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'smoke-run-' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'while \[ "\$readiness_attempts" -lt 180 \]; do' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'body\.strip\(\) == b"ready"' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'gitea-mail-launcher-up' "$root/scripts/gitea-mail-acceptance-demo.sh"
require 'CITY_MAIL_LAUNCHER_SMOKE_SKIP_UP=true' "$root/scripts/gitea-mail-acceptance-demo.sh"
require 'CITY_MAIL_LAUNCHER_SMOKE_SKIP_UP:-false' "$root/scripts/gitea-mail-launcher-smoke.sh"
require 'gitea-mail-launcher-smoke' "$root/scripts/gitea-mail-acceptance-demo.sh"
if grep -Fq 'gitea-mail-bridge-bootstrap' "$root/scripts/gitea-mail-acceptance-demo.sh"; then
  printf '%s\n' 'acceptance demo must delegate bootstrap exactly once through launcher-up' >&2
  exit 1
fi
require 'fresh disposable issue' "$root/README.md"
require 'real Mayor/formula trace remains a separate Gate D requirement' "$root/README.md"
require 'machine plan marker must contain only revision and repository JSON fields' "$root/scripts/city-mail-wake.sh"
require 'must appear only inside the standalone marker' "$root/scripts/city-mail-wake.sh"

wake_tmp="$(mktemp -d)"
trap 'rm -f "$rendered" "$bridge" "$mail" "$city"; rm -rf "$wake_tmp"' EXIT
cat >"$wake_tmp/fake-gc" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$*" >>"$CITY_MAIL_WAKE_TEST_LOG"
EOF
chmod +x "$wake_tmp/fake-gc"
printf '%s\n' '{"message":{"id":17}}' >"$wake_tmp/mayor.signal"
CITY_PATH=/city CITY_MAIL_SIGNAL_FILE="$wake_tmp/mayor.signal" \
  CITY_MAIL_WAKE_GC="$wake_tmp/fake-gc" CITY_MAIL_WAKE_TEST_LOG="$wake_tmp/log" \
  CITY_MAIL_WAKE_ONCE=true sh "$root/scripts/city-mail-wake.sh"
require '^--city /city session nudge --delivery=wait-idle mayor You have authenticated tracker mail in Agent Mail\. Fetch it through the Mayor-only Agent Mail MCP binding; follow Superpowers and IDD planning; respond on the linked Gitea issue before internal ceremony\. When publishing an authenticated intake plan, the standalone gascity:intake-plan:v1 machine plan marker must contain only revision and repository JSON fields; its reserved literal must appear only inside the standalone marker; use a new revision after every amendment\.$' "$wake_tmp/log"

printf '%s\n' 'PASS: private City mail issue ingress profile is configured'
