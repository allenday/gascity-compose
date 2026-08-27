#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
[ -r "$env_file" ] || { printf '%s\n' "ERROR: copy .env.example to $env_file first" >&2; exit 1; }

value_for() {
  awk -F= -v key="$1" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
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
  value="$(value_for "$1")"
  [ -n "$value" ] && [ "$value" != bootstrap-required ] && ! printf '%s' "$value" | grep -q '^CHANGE_ME' || {
    printf '%s\n' "ERROR: $1 must be set in $env_file" >&2
    exit 1
  }
  printf '%s' "$value"
}

compose() {
  docker compose --env-file "$env_file" "$@"
}

relay_url="$(require_value BUZZ_RELAY_URL)"
# The deployed relay advertises its public WebSocket URL, while the upstream
# host CLI speaks the same Nginx authority over HTTP(S).
cli_relay_url="$(printf '%s' "$relay_url" | sed -e 's#^ws://#http://#' -e 's#^wss://#https://#')"
channel_admin_key="$(require_value BUZZ_CHANNEL_ADMIN_PRIVATE_KEY)"
cli="$(value_for BUZZ_CLI)"
cli="${cli:-buzz}"
command -v "$cli" >/dev/null 2>&1 || {
  printf '%s\n' "ERROR: BUZZ_CLI must name the installed upstream buzz CLI" >&2
  exit 1
}

# The bridge is its own Nostr identity. Generate its complete keypair exactly
# once through the upstream relay image and retain both values in ignored .env.
bridge_private="$(value_for BUZZ_BRIDGE_PRIVATE_KEY)"
bridge_public="$(value_for BUZZ_BRIDGE_PUBLIC_KEY)"
if [ -z "$bridge_private" ] && [ -z "$bridge_public" ]; then
  compose --profile buzz up -d --wait --wait-timeout 120 buzz-relay
  generated="$(compose --profile buzz exec -T buzz-relay buzz-admin generate-key)"
  bridge_private="$(printf '%s\n' "$generated" | awk '/Secret key:/ { print $3; exit }')"
  bridge_public="$(printf '%s\n' "$generated" | awk '/Public key:/ { print $3; exit }')"
  [ -n "$bridge_private" ] && [ -n "$bridge_public" ] || {
    printf '%s\n' 'ERROR: buzz-admin did not return a bridge keypair' >&2
    exit 1
  }
  set_value BUZZ_BRIDGE_PRIVATE_KEY "$bridge_private"
  set_value BUZZ_BRIDGE_PUBLIC_KEY "$bridge_public"
elif [ -z "$bridge_private" ] || [ -z "$bridge_public" ]; then
  printf '%s\n' 'ERROR: BUZZ_BRIDGE_PRIVATE_KEY and BUZZ_BRIDGE_PUBLIC_KEY must be restored together' >&2
  exit 1
fi

channel="$(value_for BUZZ_MAYOR_CHANNEL_ID)"
if [ -z "$channel" ]; then
  channel="$(BUZZ_RELAY_URL="$cli_relay_url" BUZZ_PRIVATE_KEY="$channel_admin_key" "$cli" channels create \
    --name buzz-mayor --type stream --visibility private | jq -er '.channel_id')"
  set_value BUZZ_MAYOR_CHANNEL_ID "$channel"
else
  BUZZ_RELAY_URL="$cli_relay_url" BUZZ_PRIVATE_KEY="$channel_admin_key" "$cli" channels get --channel "$channel" |
    jq -e --arg channel "$channel" '.channel_id == $channel' >/dev/null
fi

human_keys="$(require_value BUZZ_ALLOWED_HUMAN_PUBKEYS)"
ensure_relay_member() {
  key="$1"
  if ! compose --profile buzz exec -T buzz-relay buzz-admin list-members | grep -Fqi "$key"; then
    compose --profile buzz exec -T buzz-relay buzz-admin add-member --pubkey "$key"
  fi
}
ensure_relay_member "$bridge_public"
for human_key in $(printf '%s' "$human_keys" | tr ',' ' '); do ensure_relay_member "$human_key"; done

ensure_channel_member() {
  key="$1"
  if ! BUZZ_RELAY_URL="$cli_relay_url" BUZZ_PRIVATE_KEY="$channel_admin_key" "$cli" channels members --channel "$channel" |
    jq -e --arg key "$key" 'map(.pubkey) | index($key) != null' >/dev/null; then
    BUZZ_RELAY_URL="$cli_relay_url" BUZZ_PRIVATE_KEY="$channel_admin_key" "$cli" channels add-member \
      --channel "$channel" --pubkey "$key" --role member | jq -e '.accepted == true' >/dev/null
  fi
}

ensure_channel_member "$bridge_public"
for human_key in $(printf '%s' "$human_keys" | tr ',' ' '); do
  ensure_channel_member "$human_key"
done

bearer="$(value_for MCP_AGENT_MAIL_BEARER_TOKEN)"
if [ -z "$bearer" ] || [ "$bearer" = bootstrap-required ]; then
  bearer="$(openssl rand -hex 32)"
  set_value MCP_AGENT_MAIL_BEARER_TOKEN "$bearer"
fi
compose --profile buzz --profile buzz-mayor-bridge up -d --wait --wait-timeout 90 mcp-agent-mail
mcp_call() {
  tool="$1"
  arguments="$2"
  payload="$(jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$tool,arguments:$arguments}}')"
  printf '%s' "$payload" | compose --profile buzz --profile buzz-mayor-bridge exec -T mcp-agent-mail python3 -c '
import sys, urllib.request
body = sys.stdin.buffer.read()
request = urllib.request.Request("http://127.0.0.1:8765/mcp", data=body, headers={"Authorization": "Bearer " + sys.argv[1], "Content-Type": "application/json", "Accept": "application/json"})
with urllib.request.urlopen(request, timeout=30) as response:
    sys.stdout.buffer.write(response.read())
' "$bearer" | jq -cer '
    if .error then error(.error.message)
    elif .result.isError == true then error("Agent Mail tool failed")
    elif .result.structuredContent != null then (.result.structuredContent.result // .result.structuredContent)
    elif .result.content[0].text? != null then (.result.content[0].text | fromjson)
    else .result end
  '
}
project_path="$(require_value MCP_AGENT_MAIL_PROJECT_PATH)"
project="$(mcp_call ensure_project "$(jq -nc --arg key "$project_path" '{human_key:$key}')")"
project_key="$(printf '%s' "$project" | jq -er '.slug | select(test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"))')"
set_value MCP_AGENT_MAIL_PROJECT_KEY "$project_key"
bridge_identity="$(require_value BUZZ_AGENT_MAIL_BRIDGE_IDENTITY)"
bridge_token="$(value_for BUZZ_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN)"
registered="$(mcp_call register_agent "$(jq -nc --arg project "$project_key" --arg name "$bridge_identity" --arg token "$bridge_token" \
  '{project_key:$project,program:"gas-city",model:"service",name:$name,task_description:"private Buzz Mayor relay"} + if $token == "" then {} else {registration_token:$token} end')")"
bridge_token="$(printf '%s' "$registered" | jq -er '.registration_token | select(length > 0)')"
set_value BUZZ_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN "$bridge_token"
mayor_identity="$(require_value BUZZ_AGENT_MAIL_MAYOR_IDENTITY)"
mayor_token="$(require_value MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN)"
mcp_call macro_contact_handshake "$(jq -nc --arg project "$project_key" --arg bridge "$bridge_identity" --arg bridge_token "$bridge_token" --arg mayor "$mayor_identity" --arg mayor_token "$mayor_token" \
  '{project_key:$project,requester:$bridge,target:$mayor,auto_accept:true,ttl_seconds:31536000,requester_registration_token:$bridge_token,target_registration_token:$mayor_token}')" >/dev/null
mcp_call macro_contact_handshake "$(jq -nc --arg project "$project_key" --arg bridge "$bridge_identity" --arg bridge_token "$bridge_token" --arg mayor "$mayor_identity" --arg mayor_token "$mayor_token" \
  '{project_key:$project,requester:$mayor,target:$bridge,auto_accept:true,ttl_seconds:31536000,requester_registration_token:$mayor_token,target_registration_token:$bridge_token}')" >/dev/null

mkdir -p state/buzz-mayor-bridge
chown -R 65532:65532 state/buzz-mayor-bridge
chmod 0700 state/buzz-mayor-bridge
chmod 600 "$env_file"
printf '%s\n' "PASS: private Buzz Mayor channel $channel and relay memberships are reconciled"
