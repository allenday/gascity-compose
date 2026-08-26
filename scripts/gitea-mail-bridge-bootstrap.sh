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

set_value() {
  key="$1"
  value="$2"
  tmp="$(mktemp "${env_file}.tmp.XXXXXX")"
  umask 077
  awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$env_file" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$env_file"
}

require_value() {
  key="$1"
  value="$(value_for "$key")"
  if [ -z "$value" ]; then
    printf '%s\n' "ERROR: $key must be set in $env_file" >&2
    exit 1
  fi
  printf '%s' "$value"
}

compose() {
  docker compose --env-file "$env_file" "$@"
}

mcp_call() {
  tool="$1"
  arguments="$2"
  token="$(require_value MCP_AGENT_MAIL_BEARER_TOKEN)"
  payload="$(jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$tool,arguments:$arguments}}')"
  request="$(jq -nc --arg token "$token" --argjson payload "$payload" '{token:$token,payload:$payload}')"
  response="$(printf '%s' "$request" | compose exec -T mcp-agent-mail python3 -c '
import json, sys, urllib.request
request = json.load(sys.stdin)
encoded = json.dumps(request["payload"], separators=(",", ":")).encode()
http = urllib.request.Request(
    "http://127.0.0.1:8765/mcp",
    data=encoded,
    headers={
        "Authorization": "Bearer " + request["token"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
)
with urllib.request.urlopen(http, timeout=30) as response:
    sys.stdout.buffer.write(response.read())
')"
  printf '%s' "$response" | jq -cer '
    if .error then error(.error.message)
    elif .result.isError == true then error((.result.content // [] | map(.text // "") | join("; ")))
    elif .result.structuredContent != null then (.result.structuredContent.result // .result.structuredContent)
    elif .result.content[0].text? != null then (.result.content[0].text | fromjson)
    else .result end
  '
}

random_secret() {
  openssl rand -hex 32
}

for key in MCP_AGENT_MAIL_BEARER_TOKEN GITEA_MAIL_BRIDGE_WEBHOOK_SECRET; do
  current="$(value_for "$key")"
  if [ -z "$current" ] || [ "$current" = bootstrap-required ]; then
    set_value "$key" "$(random_secret)"
  fi
done

# Establish the base stack before validating or mutating intake resources.
ENV_FILE="$env_file" sh ./scripts/bootstrap.sh

admin="$(require_value STACK_USERNAME)"
manifest_path="$(require_value INTAKE_MANIFEST_PATH)"
test -n "$manifest_path"
gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
# Keep Gitea's generated issue URLs canonical for the private bridge.  The
# operator reaches the published host port, while --connect-to preserves the
# internal Host authority that Gitea serializes into signed webhook payloads.
gitea_api="http://gitea:3000/api/v1"
curl() {
  command curl --connect-to "gitea:3000:127.0.0.1:${gitea_port}" "$@"
}

manifest_file() {
  case "$manifest_path" in
    /*) printf '%s\n' "$manifest_path" ;;
    *) printf '%s/%s\n' "$(dirname "$env_file")" "$manifest_path" ;;
  esac
}

manifest_repositories() {
  python3 - "$(manifest_file)" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    data = tomllib.load(handle)

for repository in data.get("repositories", []):
    print(repository)
PY
}

# Password API authentication is commonly disabled. Reconcile a scoped operator
# token before the read-only doctor so the fixture preflight can use the same
# exact operator authority as the mutating path.
admin_password="$(value_for GITEA_MAIL_BRIDGE_ADMIN_TOKEN)"
admin_token_version="$(value_for GITEA_MAIL_BRIDGE_ADMIN_TOKEN_VERSION)"
if [ -z "$admin_password" ] || [ "$admin_token_version" != 2 ]; then
  admin_password="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$admin" \
    --token-name gascity-mail-bridge-operator-v2 \
    --scopes 'read:user,write:user,read:repository,write:repository,read:issue,write:issue' \
    --raw)"
  test -n "$admin_password"
  set_value GITEA_MAIL_BRIDGE_ADMIN_TOKEN "$admin_password"
  set_value GITEA_MAIL_BRIDGE_ADMIN_TOKEN_VERSION 2
fi

# The disposable default fixture is the only repository bootstrap may create.
# When its exact admin/repo scope is declared in the manifest, create it before
# the doctor runs so fresh demo stacks pass read-only preflight.
fixture_repo="$(value_for GITEA_MAIL_BRIDGE_FIXTURE_REPOSITORY)"
if [ -n "$fixture_repo" ] && manifest_repositories | grep -Fxq "$admin/$fixture_repo"; then
  if ! curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$admin/$fixture_repo" >/dev/null 2>&1; then
    curl --fail --silent --show-error --user "$admin:$admin_password" \
      -H 'Content-Type: application/json' \
      -X POST "$gitea_api/user/repos" \
      --data "$(jq -nc --arg name "$fixture_repo" '{name:$name,private:true,auto_init:true,default_branch:"main",has_issues:true}')" >/dev/null
  fi
fi

doctor_output="$(ENV_FILE="$env_file" sh ./scripts/gitea-intake-doctor.sh --format env)"
for derived_key in INTAKE_ACCOUNT INTAKE_REPOSITORY_SCOPES INTAKE_CITY_IDENTITIES INTAKE_MINIMUM_REPOSITORY_ROLE; do
  derived_value="$(printf '%s\n' "$doctor_output" | awk -F= -v key="$derived_key" '$1 == key { print substr($0, length(key) + 2); exit }')"
  [ -n "$derived_value" ] || {
    printf '%s\n' "ERROR: intake doctor did not return $derived_key" >&2
    exit 1
  }
  set_value "$derived_key" "$derived_value"
done

intake_account="$(require_value INTAKE_ACCOUNT)"
bridge_login="gascity-mail-bridge"
launcher_login="gascity-mail-launcher"

# Stop any prior bridge before taking ownership of its durable ledger. Otherwise
# an older root-run container can atomically replace the file during migration.
compose --profile gitea-mail-bridge stop gitea-mail-bridge

# The bridge always runs as the distroless non-root identity. Keeping its ledger
# owned by that identity makes the fixture's read-only persistence check real.
mkdir -p state/gitea-mail-bridge state/city-mail-secrets
chown -R 65532:65532 state/gitea-mail-bridge
chmod 0700 state/gitea-mail-bridge state/city-mail-secrets

compose --profile mcp up -d --wait --wait-timeout 90 mcp-agent-mail

user_record() {
  compose exec -T gitea gitea admin user list | awk -v login="$1" '$2 == login { print $1; exit }'
}
for identity in "$intake_account" "$bridge_login" "$launcher_login"; do
  if [ -z "$(user_record "$identity")" ]; then
    password="$(random_secret)"
    compose exec -T gitea gitea admin user create \
      --username "$identity" \
      --email "${identity}@localhost" \
      --password "$password" \
      --must-change-password=false \
      --restricted >/dev/null
  fi
done

if [ -z "$(value_for GITEA_MAIL_BRIDGE_TOKEN)" ]; then
  token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$bridge_login" \
    --token-name gascity-mail-bridge \
    --scopes 'read:user,read:repository,read:issue' \
    --raw)"
  test -n "$token"
  set_value GITEA_MAIL_BRIDGE_TOKEN "$token"
fi

# Mayor's public tracker identity remains the existing role-scoped Gitea MCP
# account. Bootstrap its write token only when this deployment has none.
if [ -z "$(value_for GITEA_MAYOR_TOKEN)" ]; then
  token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$intake_account" \
    --token-name gascity-mcp-mayor \
    --scopes 'read:user,read:repository,read:issue,write:issue' \
    --raw)"
  test -n "$token"
  set_value GITEA_MAYOR_TOKEN "$token"
fi

mayor_actor_id="$(user_record "$intake_account")"
test -n "$mayor_actor_id"
set_value INTAKE_MAYOR_ACTOR_ID "$mayor_actor_id"

webhook_url="http://gitea-mail-bridge:8080/v1/gitea/webhook"
webhook_secret="$(require_value GITEA_MAIL_BRIDGE_WEBHOOK_SECRET)"
approval_label="$(require_value INTAKE_APPROVAL_LABEL)"
# Reconcile only the repositories still declared in the current manifest. A
# later manifest removal stops new intake after redeploy, but leaves prior
# labels, webhooks, and ledger history intact as durable evidence.
for repository in $(printf '%s' "$(require_value INTAKE_REPOSITORY_SCOPES)" | tr ',' ' '); do
  owner="${repository%%/*}"
  repo="${repository#*/}"
  if [ -z "$owner" ] || [ -z "$repo" ] || [ "$owner" = "$repo" ]; then
    printf '%s\n' "ERROR: invalid INTAKE_REPOSITORY_SCOPES entry: $repository" >&2
    exit 1
  fi
  curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$owner/$repo" >/dev/null

  curl --fail --silent --show-error --user "$admin:$admin_password" \
    -H 'Content-Type: application/json' -X PUT \
    "$gitea_api/repos/$owner/$repo/collaborators/$bridge_login" \
    --data '{"permission":"read"}' >/dev/null
  curl --fail --silent --show-error --user "$admin:$admin_password" \
    -H 'Content-Type: application/json' -X PUT \
    "$gitea_api/repos/$owner/$repo/collaborators/$intake_account" \
    --data '{"permission":"write"}' >/dev/null
  bridge_permission="$(curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$owner/$repo/collaborators/$bridge_login/permission")"
  mayor_permission="$(curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$owner/$repo/collaborators/$intake_account/permission")"
  printf '%s' "$bridge_permission" | jq -e '.permission == "read"' >/dev/null
  printf '%s' "$mayor_permission" | jq -e '.permission == "write"' >/dev/null

  labels="$(curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$owner/$repo/labels?limit=100")"
  for label in gc:city-managed "$approval_label"; do
    if ! printf '%s' "$labels" | jq -e --arg label "$label" '.[] | select(.name == $label)' >/dev/null; then
      curl --fail --silent --show-error --user "$admin:$admin_password" \
        -H 'Content-Type: application/json' \
        -X POST "$gitea_api/repos/$owner/$repo/labels" \
        --data "$(jq -nc --arg name "$label" '{name:$name,color:"0052cc",description:"Gas City intake lifecycle marker",exclusive:false}')" >/dev/null
    fi
  done

  hooks="$(curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$owner/$repo/hooks?limit=100")"
  hook_ids="$(printf '%s' "$hooks" | jq -r --arg url "$webhook_url" '.[] | select(.config.url == $url) | .id')"
  hook_id="$(printf '%s\n' "$hook_ids" | sed -n '1p')"
  hook_payload="$(jq -nc --arg url "$webhook_url" --arg secret "$webhook_secret" \
    '{type:"gitea",active:true,branch_filter:"*",config:{url:$url,content_type:"json",secret:$secret},events:["issues","issue_assign","issue_comment","issue_label"]}')"
  if [ -n "$hook_id" ]; then
    curl --fail --silent --show-error --user "$admin:$admin_password" \
      -H 'Content-Type: application/json' \
      -X PATCH "$gitea_api/repos/$owner/$repo/hooks/$hook_id" \
      --data "$hook_payload" >/dev/null
  else
    curl --fail --silent --show-error --user "$admin:$admin_password" \
      -H 'Content-Type: application/json' \
      -X POST "$gitea_api/repos/$owner/$repo/hooks" \
      --data "$hook_payload" >/dev/null
  fi
  printf '%s\n' "$hook_ids" | sed '1d' | while IFS= read -r duplicate_hook_id; do
    [ -n "$duplicate_hook_id" ] || continue
    curl --fail --silent --show-error --user "$admin:$admin_password" \
      -X DELETE "$gitea_api/repos/$owner/$repo/hooks/$duplicate_hook_id" >/dev/null
  done
done

bridge_gitea_token="$(require_value GITEA_MAIL_BRIDGE_TOKEN)"
mayor_gitea_token="$(require_value GITEA_MAYOR_TOKEN)"
curl --fail --silent --show-error -H "Authorization: token $bridge_gitea_token" "$gitea_api/user" |
  jq -e --arg login "$bridge_login" '.login == $login and .is_admin == false and .restricted == true' >/dev/null
curl --fail --silent --show-error -H "Authorization: token $mayor_gitea_token" "$gitea_api/user" |
  jq -e --arg login "$intake_account" '.login == $login and .is_admin == false and .restricted == true' >/dev/null

project_path="$(require_value MCP_AGENT_MAIL_PROJECT_PATH)"
project="$(mcp_call ensure_project "$(jq -nc --arg key "$project_path" '{human_key:$key}')")"
project_key="$(printf '%s' "$project" | jq -er '.slug | select(test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"))')"
set_value MCP_AGENT_MAIL_PROJECT_KEY "$project_key"

register_identity() {
  name="$1"
  env_key="$2"
  existing="$(value_for "$env_key")"
  arguments="$(jq -nc --arg project "$project_key" --arg name "$name" --arg token "$existing" \
    '{project_key:$project,program:"gas-city",model:"service",name:$name,task_description:"City tracker intake"} + if $token == "" then {} else {registration_token:$token} end')"
  registered="$(mcp_call register_agent "$arguments")"
  token="$(printf '%s' "$registered" | jq -er '.registration_token | select(length > 0)')"
  set_value "$env_key" "$token"
}

bridge_identity="$(require_value INTAKE_MAIL_BRIDGE_IDENTITY)"
mayor_identity="$(require_value INTAKE_MAIL_MAYOR_IDENTITY)"
launcher_identity="$(require_value INTAKE_MAIL_LAUNCHER_IDENTITY)"
register_identity "$bridge_identity" MCP_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN
register_identity "$mayor_identity" MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN
register_identity "$launcher_identity" MCP_AGENT_MAIL_LAUNCHER_REGISTRATION_TOKEN

authorize_contact() {
  from="$1"
  from_token="$2"
  to="$3"
  to_token="$4"
  mcp_call macro_contact_handshake "$(jq -nc \
    --arg project "$project_key" --arg from "$from" --arg from_token "$from_token" \
    --arg to "$to" --arg to_token "$to_token" \
    '{project_key:$project,requester:$from,target:$to,auto_accept:true,ttl_seconds:31536000,requester_registration_token:$from_token,target_registration_token:$to_token}')" >/dev/null
}

bridge_token="$(require_value MCP_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN)"
mayor_token="$(require_value MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN)"
launcher_token="$(require_value MCP_AGENT_MAIL_LAUNCHER_REGISTRATION_TOKEN)"
authorize_contact "$bridge_identity" "$bridge_token" "$mayor_identity" "$mayor_token"
authorize_contact "$mayor_identity" "$mayor_token" "$bridge_identity" "$bridge_token"
authorize_contact "$bridge_identity" "$bridge_token" "$launcher_identity" "$launcher_token"
authorize_contact "$launcher_identity" "$launcher_token" "$bridge_identity" "$bridge_token"

mayor_secret="state/city-mail-secrets/mayor.env"
mayor_secret_tmp="$(mktemp "${mayor_secret}.tmp.XXXXXX")"
umask 077
{
  printf 'MCP_AGENT_MAIL_BEARER_TOKEN=%s\n' "$(require_value MCP_AGENT_MAIL_BEARER_TOKEN)"
  printf 'MCP_AGENT_MAIL_REGISTRATION_TOKEN=%s\n' "$mayor_token"
  printf 'MCP_AGENT_MAIL_PROJECT_KEY=%s\n' "$project_key"
  printf 'MCP_AGENT_MAIL_AGENT_NAME=%s\n' "$mayor_identity"
} >"$mayor_secret_tmp"
chmod 0600 "$mayor_secret_tmp"
mv "$mayor_secret_tmp" "$mayor_secret"

launcher_secret="state/city-mail-secrets/launcher.env"
launcher_secret_tmp="$(mktemp "${launcher_secret}.tmp.XXXXXX")"
umask 077
{
  printf 'MCP_AGENT_MAIL_BEARER_TOKEN=%s\n' "$(require_value MCP_AGENT_MAIL_BEARER_TOKEN)"
  printf 'MCP_AGENT_MAIL_REGISTRATION_TOKEN=%s\n' "$launcher_token"
  printf 'MCP_AGENT_MAIL_PROJECT_KEY=%s\n' "$project_key"
  printf 'MCP_AGENT_MAIL_AGENT_NAME=%s\n' "$launcher_identity"
} >"$launcher_secret_tmp"
chmod 0600 "$launcher_secret_tmp"
mv "$launcher_secret_tmp" "$launcher_secret"
mkdir -p state/city-mail-launcher
launcher_uid="$(value_for HOST_UID)"; launcher_uid="${launcher_uid:-1000}"
launcher_gid="$(value_for HOST_GID)"; launcher_gid="${launcher_gid:-1000}"
case "$launcher_uid:$launcher_gid" in
  *[!0-9:]* | :* | *:) printf '%s\n' 'ERROR: HOST_UID and HOST_GID must be numeric when set' >&2; exit 1 ;;
esac
chown -R "$launcher_uid:$launcher_gid" state/city-mail-launcher
chmod 0700 state/city-mail-launcher

chmod 600 "$env_file"
printf '%s\n' 'PASS: City mail bridge identities, contacts, labels, and signed webhooks are reconciled'
