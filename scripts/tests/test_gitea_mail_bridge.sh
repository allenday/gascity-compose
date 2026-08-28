#!/usr/bin/env sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
rendered="$(mktemp)"
bridge="$(mktemp)"
mail="$(mktemp)"
city="$(mktemp)"
launcher="$(mktemp)"
permission_reader="$(mktemp)"
doctor_tmp="$(mktemp -d)"
trap 'rm -f "$rendered" "$bridge" "$mail" "$city" "$launcher" "$permission_reader"; rm -rf "$doctor_tmp"' EXIT

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
service_block gitea-intake-permission-reader "$permission_reader"

# The intake bridge is a private, independently stateful process with only
# read-side Gitea and authenticated Agent Mail capabilities.
require '^  gitea-mail-bridge:$' "$bridge"
require 'user: 65532:65532' "$bridge"
require 'source: .*/state/gitea-mail-bridge' "$bridge"
require 'target: /var/lib/gitea-mail-bridge' "$bridge"
require 'INTAKE_LEDGER_PATH: /var/lib/gitea-mail-bridge/ledger.json' "$bridge"
require 'GITEA_URL: http://gitea:3000' "$bridge"
require 'INTAKE_MAIL_URL: http://mcp-agent-mail:8765/mcp' "$bridge"
require 'INTAKE_PERMISSION_READER_URL: http://gitea-intake-permission-reader:8080/v1/repository-role' "$bridge"
require 'INTAKE_PERMISSION_READER_BEARER_TOKEN:' "$bridge"
require 'gitea:' "$bridge"
require 'condition: service_healthy' "$bridge"
require 'mcp-agent-mail:' "$bridge"
require 'gitea-intake-permission-reader:' "$bridge"
require 'INTAKE_MINIMUM_REPOSITORY_ROLE: triage' "$bridge"
if grep -Eq '^    ports:' "$bridge"; then
  printf '%s\n' 'gitea-mail-bridge must not publish a host port' >&2
  exit 1
fi
if grep -Eq 'GASCITY_API_URL|GASCITY_RUN_ID|GITEA_ISSUE_URL|GITEA_MAIL_BRIDGE_ADMIN_TOKEN|GITEA_INTAKE_PERMISSION_READER_TOKEN' "$bridge"; then
  printf '%s\n' 'gitea-mail-bridge must not receive City mutation or status-bridge bindings' >&2
  exit 1
fi
if grep -Eq 'INTAKE_ELIGIBLE_COLLABORATORS' "$bridge"; then
  printf '%s\n' 'gitea-mail-bridge must not accept operator-maintained human eligibility lists' >&2
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

# The permission reader is a private bridge-only role lookup proxy. It keeps a
# scoped admin PAT in a secret file and exposes only a bearer-gated internal
# permission endpoint for declared repositories.
require '^  gitea-intake-permission-reader:$' "$permission_reader"
require 'target: /run/secrets/city-mail/permission-reader.env' "$permission_reader"
require 'read_only: true' "$permission_reader"
require 'GITEA_URL: http://gitea:3000' "$permission_reader"
require 'INTAKE_REPOSITORY_SCOPES:' "$permission_reader"
require 'gitea:' "$permission_reader"
require 'condition: service_healthy' "$permission_reader"
if grep -Eq '^    ports:|INTAKE_MAIL_|GASCITY_|GITEA_MAIL_BRIDGE_WEBHOOK_SECRET|GITEA_MAYOR_TOKEN|GITEA_MAIL_BRIDGE_TOKEN:' "$permission_reader"; then
  printf '%s\n' 'permission reader must remain private and hold only its scoped read-side authority' >&2
  exit 1
fi

# Gitea may deliver only to the internal bridge hostname, and every profile
# must use the reviewed immutable core revision.
require 'GITEA__webhook__ALLOWED_HOST_LIST: 127.0.0.1,localhost,woodpecker-server,gitea-mail-bridge' "$rendered"
require 'GITEA__security__ALLOWED_HOST_LIST: loopback,woodpecker-server,gitea-mail-bridge' "$rendered"
require 'GASCITY_GITEA_REF: 827d768468a76787655ef46be24679301dc7e217' "$rendered"
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
require 'sh ./scripts/gitea-intake-doctor.sh' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'INTAKE_MANIFEST_PATH' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'INTAKE_MINIMUM_REPOSITORY_ROLE' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
agent_mail_stop_line="$(grep -n 'stop mcp-agent-mail' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
bootstrap_line="$(grep -n 'sh ./scripts/bootstrap.sh' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
if [ -z "$agent_mail_stop_line" ] || [ -z "$bootstrap_line" ] || [ "$agent_mail_stop_line" -ge "$bootstrap_line" ]; then
  printf '%s\n' 'bootstrap must quiesce Agent Mail before reconciling its state ownership' >&2
  exit 1
fi
doctor_line="$(grep -n 'sh ./scripts/gitea-intake-doctor.sh --format env' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
operator_token_line="$(grep -n 'gascity-mail-bridge-operator-v2' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
fixture_create_line="$(grep -n '\$gitea_api/user/repos' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
user_create_line="$(grep -n 'gitea admin user create' "$root/scripts/gitea-mail-bridge-bootstrap.sh" | head -n 1 | cut -d: -f1)"
if [ -z "$doctor_line" ] || [ -z "$operator_token_line" ] || [ -z "$fixture_create_line" ] || [ -z "$user_create_line" ]; then
  printf '%s\n' 'bootstrap ordering probes are missing' >&2
  exit 1
fi
if [ "$operator_token_line" -ge "$doctor_line" ]; then
  printf '%s\n' 'bootstrap must reconcile the operator token before running the intake doctor' >&2
  exit 1
fi
if [ "$fixture_create_line" -ge "$doctor_line" ]; then
  printf '%s\n' 'bootstrap must create the declared default fixture repository before running the intake doctor' >&2
  exit 1
fi
if [ "$doctor_line" -ge "$user_create_line" ]; then
  printf '%s\n' 'bootstrap must run the read-only intake doctor before role and repository mutations' >&2
  exit 1
fi
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
require 'city-mail-secrets/permission-reader.env' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'GITEA_INTAKE_PERMISSION_READER_TOKEN' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'GITEA_INTAKE_PERMISSION_READER_TOKEN_VERSION' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'GITEA_INTAKE_PERMISSION_READER_BEARER_TOKEN' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'read:user,read:repository' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'city-mail-wake' "$root/docker/city-entrypoint.sh"
require 'city-mail-mcp-proxy' "$root/scripts/codex-mayor"
require '/usr/local/bin/codex "\$@" </dev/tty &' "$root/scripts/codex-mayor"
require 'COPY scripts/city-mail-mcp-proxy.py /usr/local/bin/city-mail-mcp-proxy' "$root/Dockerfile.city"
require 'bash ca-certificates curl git jq libicu72 python3 tini' "$root/Dockerfile.city"
require 'timeout --signal=TERM' "$root/scripts/city-mail-wake.sh"
require 'collaborators/\$bridge_login' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'permission":"read' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'collaborators/\$intake_account' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'permission":"write' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'gascity-mcp-mayor' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'gascity-mail-bridge' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'gascity-mail-launcher' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require 'token_belongs_to "\$bridge_gitea_token" "\$bridge_login"' "$root/scripts/gitea-mail-bridge-bootstrap.sh"
require '^gitea-mail-bridge-bootstrap:' "$root/Makefile"
require '^gitea-mail-bridge-up:' "$root/Makefile"
require '^gitea-mail-bridge-smoke:' "$root/Makefile"
require '^gitea-mail-launcher-up:' "$root/Makefile"
require '^gitea-mail-launcher-smoke:' "$root/Makefile"
require '^gitea-mail-acceptance-demo:' "$root/Makefile"
require '^gitea-intake-doctor:' "$root/Makefile"
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
require '^INTAKE_MANIFEST_PATH=./config/gitea-intake.toml$' "$root/.env.example"
require '^INTAKE_REPOSITORY_SCOPES=$' "$root/.env.example"
require '^INTAKE_CITY_IDENTITIES=$' "$root/.env.example"
require '^INTAKE_MINIMUM_REPOSITORY_ROLE=$' "$root/.env.example"
require '^GITEA_INTAKE_PERMISSION_READER_TOKEN=$' "$root/.env.example"
require '^GITEA_INTAKE_PERMISSION_READER_TOKEN_VERSION=1$' "$root/.env.example"
require '^GITEA_INTAKE_PERMISSION_READER_BEARER_TOKEN=$' "$root/.env.example"
if grep -Eq '^INTAKE_ELIGIBLE_COLLABORATORS=' "$root/.env.example"; then
  printf '%s\n' '.env.example must not define operator-maintained approval identities' >&2
  exit 1
fi
require '^minimum_repository_role = "triage"$' "$root/config/gitea-intake.toml"
require '^repositories = \["admin/gascity-intake-fixture"\]$' "$root/config/gitea-intake.toml"
require 'fresh disposable issue' "$root/README.md"
require 'real Mayor/formula trace remains a separate Gate D requirement' "$root/README.md"
require 'INTAKE_MANIFEST_PATH' "$root/README.md"
require 'gitea-intake-doctor' "$root/README.md"
require 'current non-City Gitea collaborator with `triage` or stronger' "$root/README.md"
require 'Removing a repository from the manifest stops new intake after redeploy' "$root/README.md"
require 'does not delete prior labels, webhooks, users, or ledger history' "$root/README.md"
require 'machine plan marker must contain only revision and repository JSON fields' "$root/scripts/city-mail-wake.sh"
require 'must appear only inside the standalone marker' "$root/scripts/city-mail-wake.sh"

wake_tmp="$(mktemp -d)"
trap 'rm -f "$rendered" "$bridge" "$mail" "$city" "$launcher"; rm -rf "$doctor_tmp" "$wake_tmp"' EXIT
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

cat >"$doctor_tmp/env" <<'EOF'
STACK_USERNAME=admin
GITEA_HTTP_PORT=3002
GITEA_MAIL_BRIDGE_ADMIN_TOKEN=test-admin-token
INTAKE_MANIFEST_PATH=./manifest.toml
EOF
cat >"$doctor_tmp/manifest.toml" <<'EOF'
minimum_repository_role = "triage"
repositories = ["admin/clean"]
EOF
cat >"$doctor_tmp/fake-curl" <<'EOF'
#!/usr/bin/env sh
set -eu
for url do :; done
case "$url" in
  */api/v1/user)
    printf '%s\n' '{"login":"admin","is_admin":true}'
    ;;
  */api/v1/repos/admin/clean)
    printf '%s\n' '{"private":true,"internal":false,"has_issues":true}'
    ;;
  */api/v1/repos/admin/clean/issues\?state=all\&limit=1)
    printf '%s\n' '[]'
    ;;
  */api/v1/users/gascity-mcp-mayor|*/api/v1/users/gascity-mail-bridge|*/api/v1/users/gascity-mail-launcher)
    exit 22
    ;;
  *)
    printf 'unexpected curl url: %s\n' "$url" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$doctor_tmp/fake-curl"
doctor_env="$(
  ENV_FILE="$doctor_tmp/env" CURL_BIN="$doctor_tmp/fake-curl" GITEA_API_ROOT='http://example.test/api/v1' \
    sh "$root/scripts/gitea-intake-doctor.sh" --format env
)"
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_ACCOUNT=gascity-mcp-mayor' >/dev/null
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_REPOSITORY_SCOPES=admin/clean' >/dev/null
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_CITY_IDENTITIES=gascity-mcp-mayor,gascity-mail-bridge,gascity-mail-launcher' >/dev/null
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_MINIMUM_REPOSITORY_ROLE=triage' >/dev/null

cat >"$doctor_tmp/manifest.toml" <<'EOF'
minimum_repository_role = "triage"
repositories = ["admin/dirty"]
EOF
cat >"$doctor_tmp/fake-curl" <<'EOF'
#!/usr/bin/env sh
set -eu
for url do :; done
case "$url" in
  */api/v1/user)
    printf '%s\n' '{"login":"admin","is_admin":true}'
    ;;
  */api/v1/repos/admin/dirty)
    printf '%s\n' '{"private":true,"internal":false,"has_issues":true}'
    ;;
  */api/v1/repos/admin/dirty/issues\?state=all\&limit=1)
    printf '%s\n' '[{"number":1}]'
    ;;
  */api/v1/users/gascity-mcp-mayor|*/api/v1/users/gascity-mail-bridge|*/api/v1/users/gascity-mail-launcher)
    exit 22
    ;;
  *)
    printf 'unexpected curl url: %s\n' "$url" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$doctor_tmp/fake-curl"
if ENV_FILE="$doctor_tmp/env" CURL_BIN="$doctor_tmp/fake-curl" GITEA_API_ROOT='http://example.test/api/v1' \
  sh "$root/scripts/gitea-intake-doctor.sh" --format env >"$doctor_tmp/doctor.out" 2>"$doctor_tmp/doctor.err"; then
  printf '%s\n' 'doctor must reject repositories with existing issue history' >&2
  exit 1
fi
require 'must have no existing issues' "$doctor_tmp/doctor.err"

cat >"$doctor_tmp/manifest.toml" <<'EOF'
minimum_repository_role = "triage"
repositories = ["admin/clean"]
EOF
cat >"$doctor_tmp/fake-curl" <<'EOF'
#!/usr/bin/env sh
set -eu
for url do :; done
case "$url" in
  */api/v1/user)
    printf '%s\n' '{"login":"admin","is_admin":true}'
    ;;
  */api/v1/repos/admin/clean)
    printf '%s\n' '{"private":true,"internal":false,"has_issues":true}'
    ;;
  */api/v1/repos/admin/clean/issues\?state=all\&limit=1)
    printf '%s\n' '[]'
    ;;
  */api/v1/users/gascity-mcp-mayor)
    exit 22
    ;;
  */api/v1/users/gascity-mail-bridge)
    printf '%s\n' '{"login":"gascity-mail-bridge","restricted":false,"is_admin":false}'
    ;;
  */api/v1/users/gascity-mail-launcher)
    exit 22
    ;;
  *)
    printf 'unexpected curl url: %s\n' "$url" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$doctor_tmp/fake-curl"
if ENV_FILE="$doctor_tmp/env" CURL_BIN="$doctor_tmp/fake-curl" GITEA_API_ROOT='http://example.test/api/v1' \
  sh "$root/scripts/gitea-intake-doctor.sh" --format env >"$doctor_tmp/doctor.out" 2>"$doctor_tmp/doctor.err"; then
  printf '%s\n' 'doctor must reject misconfigured fixed role accounts' >&2
  exit 1
fi
require 'must be restricted and non-admin' "$doctor_tmp/doctor.err"

cat >"$doctor_tmp/manifest.toml" <<'EOF'
minimum_repository_role = "triage"
repositories = ["admin/keep","admin/remove"]
EOF
cat >"$doctor_tmp/fake-curl" <<'EOF'
#!/usr/bin/env sh
set -eu
for url do :; done
printf 'GET %s\n' "$url" >>"$DOCTOR_LOG"
case "$url" in
  */api/v1/user)
    printf '%s\n' '{"login":"admin","is_admin":true}'
    ;;
  */api/v1/repos/admin/keep|*/api/v1/repos/admin/remove)
    printf '%s\n' '{"private":true,"internal":false,"has_issues":true}'
    ;;
  */api/v1/repos/admin/keep/issues\?state=all\&limit=1|*/api/v1/repos/admin/remove/issues\?state=all\&limit=1)
    printf '%s\n' '[]'
    ;;
  */api/v1/users/gascity-mcp-mayor|*/api/v1/users/gascity-mail-bridge|*/api/v1/users/gascity-mail-launcher)
    exit 22
    ;;
  *)
    printf 'unexpected curl url: %s\n' "$url" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$doctor_tmp/fake-curl"
: >"$doctor_tmp/doctor.log"
doctor_env="$(
  DOCTOR_LOG="$doctor_tmp/doctor.log" ENV_FILE="$doctor_tmp/env" CURL_BIN="$doctor_tmp/fake-curl" GITEA_API_ROOT='http://example.test/api/v1' \
    sh "$root/scripts/gitea-intake-doctor.sh" --format env
)"
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_REPOSITORY_SCOPES=admin/keep,admin/remove' >/dev/null
cat >"$doctor_tmp/manifest.toml" <<'EOF'
minimum_repository_role = "triage"
repositories = ["admin/keep"]
EOF
: >"$doctor_tmp/doctor.log"
doctor_env="$(
  DOCTOR_LOG="$doctor_tmp/doctor.log" ENV_FILE="$doctor_tmp/env" CURL_BIN="$doctor_tmp/fake-curl" GITEA_API_ROOT='http://example.test/api/v1' \
    sh "$root/scripts/gitea-intake-doctor.sh" --format env
)"
printf '%s\n' "$doctor_env" | grep -Fx 'INTAKE_REPOSITORY_SCOPES=admin/keep' >/dev/null
if printf '%s\n' "$doctor_env" | grep -Fq 'admin/remove'; then
  printf '%s\n' 'doctor must drop removed repositories from derived INTAKE_REPOSITORY_SCOPES' >&2
  exit 1
fi
if grep -Eq '^(DELETE|PATCH|POST) ' "$doctor_tmp/doctor.log"; then
  printf '%s\n' 'doctor must remain read-only during manifest removal checks' >&2
  exit 1
fi

printf '%s\n' 'PASS: private City mail issue ingress profile is configured'
