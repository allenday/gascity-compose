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

for key in INTAKE_REPOSITORY_SCOPES INTAKE_CITY_IDENTITIES INTAKE_ELIGIBLE_COLLABORATORS; do
  require_value "$key" >/dev/null
done

# The bridge always runs as the distroless non-root identity. Keeping its ledger
# owned by that identity makes the fixture's read-only persistence check real.
mkdir -p state/gitea-mail-bridge state/city-mail-secrets
chown -R 65532:65532 state/gitea-mail-bridge
chmod 0700 state/gitea-mail-bridge state/city-mail-secrets

# Establish both private dependencies before using their administrative APIs.
ENV_FILE="$env_file" sh ./scripts/bootstrap.sh
compose --profile mcp up -d --wait --wait-timeout 90 mcp-agent-mail

admin="$(require_value STACK_USERNAME)"
# Password API authentication is commonly disabled. Use a private, scoped
# operator token instead; Gitea accepts it in the existing Basic request form.
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
gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
gitea_api="http://127.0.0.1:${gitea_port}/api/v1"

intake_account="$(require_value INTAKE_ACCOUNT)"
bridge_login="gas-city-mail-bridge"
user_record() {
  compose exec -T gitea gitea admin user list | awk -v login="$1" '$2 == login { print $1; exit }'
}
for identity in "$intake_account" "$bridge_login"; do
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
    --token-name gas-city-mail-bridge \
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

# The example scope is a disposable repository. Production scopes must already
# exist; bootstrap never creates an arbitrary repository named by policy.
fixture_repo="$(value_for GITEA_MAIL_BRIDGE_FIXTURE_REPOSITORY)"
if [ -n "$fixture_repo" ] && printf '%s' "$(require_value INTAKE_REPOSITORY_SCOPES)" | tr ',' '\n' | grep -Fxq "$admin/$fixture_repo"; then
  if ! curl --fail --silent --show-error --user "$admin:$admin_password" \
    "$gitea_api/repos/$admin/$fixture_repo" >/dev/null 2>&1; then
    curl --fail --silent --show-error --user "$admin:$admin_password" \
      -H 'Content-Type: application/json' \
      -X POST "$gitea_api/user/repos" \
      --data "$(jq -nc --arg name "$fixture_repo" '{name:$name,private:true,auto_init:true,default_branch:"main",has_issues:true}')" >/dev/null
  fi
fi

webhook_url="http://gitea-mail-bridge:8080/v1/gitea/webhook"
webhook_secret="$(require_value GITEA_MAIL_BRIDGE_WEBHOOK_SECRET)"
approval_label="$(require_value INTAKE_APPROVAL_LABEL)"
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

chmod 600 "$env_file"
printf '%s\n' 'PASS: City mail bridge identities, contacts, labels, and signed webhooks are reconciled'
