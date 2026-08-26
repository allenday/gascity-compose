#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file" >&2
  exit 1
fi

issue_number=
gitea_api=
repository=
admin=
admin_password=
cleanup() {
  chmod 0700 state/gitea-mail-bridge 2>/dev/null || true
  if [ -n "$issue_number" ] && [ -n "$gitea_api" ] && [ -n "$repository" ] && [ -n "$admin" ] && [ -n "$admin_password" ]; then
    curl --silent --show-error --user "$admin:$admin_password" -H 'Content-Type: application/json' \
      -X PATCH "$gitea_api/repos/$repository/issues/$issue_number" --data '{"state":"closed"}' >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
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

sha256_hex() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    sha256sum | awk '{print $1}'
  fi
}

mcp_call() {
  tool="$1"
  arguments="$2"
  payload="$(jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
    '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$tool,arguments:$arguments}}')"
  request="$(jq -nc --arg token "$(require_value MCP_AGENT_MAIL_BEARER_TOKEN)" --argjson payload "$payload" '{token:$token,payload:$payload}')"
  response="$(printf '%s' "$request" | compose exec -T mcp-agent-mail python3 -c '
import json, sys, urllib.request
request = json.load(sys.stdin)
encoded = json.dumps(request["payload"], separators=(",", ":")).encode()
http = urllib.request.Request("http://127.0.0.1:8765/mcp", data=encoded, headers={
    "Authorization": "Bearer " + request["token"],
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
})
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

fetch_inbox() {
  identity="$1"
  token="$2"
  topic="${3:-}"
  arguments="$(jq -nc --arg project "$(require_value MCP_AGENT_MAIL_PROJECT_KEY)" \
    --arg identity "$identity" --arg token "$token" --arg topic "$topic" \
    '{project_key:$project,agent_name:$identity,registration_token:$token,limit:1000,include_bodies:true} + if $topic == "" then {} else {topic:$topic} end')"
  mcp_call fetch_inbox "$arguments"
}

tracker_count() {
  inbox="$1"
  thread="$2"
  printf '%s' "$inbox" | jq -r --arg thread "$thread" \
    '[.[] | select(.thread_id == $thread) | select((.body_md | fromjson? | .type) == "gc.tracker.event.v1")] | length'
}

wait_for_count() {
  identity="$1"
  token="$2"
  thread="$3"
  wanted="$4"
  attempts=0
  while [ "$attempts" -lt 90 ]; do
    inbox="$(fetch_inbox "$identity" "$token")"
    count="$(tracker_count "$inbox" "$thread")"
    if [ "$count" -eq "$wanted" ]; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  printf '%s\n' "ERROR: tracker mail count for $thread was $count; expected $wanted" >&2
  return 1
}

wait_for_authorization() {
  topic_prefix="$1"
  attempts=0
  while [ "$attempts" -lt 90 ]; do
    inbox="$(fetch_inbox "$(require_value INTAKE_MAIL_LAUNCHER_IDENTITY)" "$(require_value MCP_AGENT_MAIL_LAUNCHER_REGISTRATION_TOKEN)")"
    authorization="$(printf '%s' "$inbox" | jq -cer --arg thread "$thread_id" --arg prefix "$topic_prefix" \
      '[.[] | select(.thread_id == $thread) | select(.subject | startswith($prefix)) | .body_md | fromjson] | first // empty' 2>/dev/null || true)"
    if [ -n "$authorization" ]; then
      printf '%s' "$authorization"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  printf '%s\n' 'ERROR: launcher did not receive a start authorization' >&2
  return 1
}

admin="$(require_value STACK_USERNAME)"
admin_password="$(require_value GITEA_MAIL_BRIDGE_ADMIN_TOKEN)"
gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
# Route through the host-published port without changing the Host authority
# embedded in signed Gitea webhook payloads.
gitea_api="http://gitea:3000/api/v1"
curl() {
  command curl --connect-to "gitea:3000:127.0.0.1:${gitea_port}" "$@"
}
fixture_repo="$(require_value GITEA_MAIL_BRIDGE_FIXTURE_REPOSITORY)"
repository="$admin/$fixture_repo"
if ! printf '%s' "$(require_value INTAKE_REPOSITORY_SCOPES)" | tr ',' '\n' | grep -Fxq "$repository"; then
  printf '%s\n' "ERROR: fixture repository $repository must be in INTAKE_REPOSITORY_SCOPES" >&2
  exit 1
fi

ENV_FILE="$env_file" sh ./scripts/gitea-mail-bridge-bootstrap.sh
compose --profile mcp --profile gitea-mail-bridge up -d --build --wait --wait-timeout 120 gitea-mail-bridge

mayor_login="$(require_value INTAKE_ACCOUNT)"
mayor_token="$(require_value GITEA_MAYOR_TOKEN)"
mayor_mail="$(require_value INTAKE_MAIL_MAYOR_IDENTITY)"
mayor_mail_token="$(require_value MCP_AGENT_MAIL_MAYOR_REGISTRATION_TOKEN)"
title="City mail smoke $(date -u +%Y%m%dT%H%M%SZ)-$$"
issue="$(curl --fail --silent --show-error --user "$admin:$admin_password" \
  -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues" \
  --data "$(jq -nc --arg title "$title" --arg mayor "$mayor_login" '{title:$title,body:"Disposable City mail ingress fixture",assignees:[$mayor]}')")"
issue_number="$(printf '%s' "$issue" | jq -er '.number')"
created_at="$(printf '%s' "$issue" | jq -er '.created_at')"
updated_at="$(printf '%s' "$issue" | jq -er '.updated_at')"
issue_url="$(printf '%s' "$issue" | jq -er '.html_url')"
admin_id="$(curl --fail --silent --show-error --user "$admin:$admin_password" "$gitea_api/users/$admin" | jq -er '.id')"

instance="http://gitea:3000"
issue_key="${#instance}:$instance${#repository}:$repository#$issue_number"
thread_id="gc.tracker.$(printf '%s' "$issue_key" | sha256_hex)"
wait_for_count "$mayor_mail" "$mayor_mail_token" "$thread_id" 1

# Re-submit the authenticated issue hint with a different delivery receipt.
# The logical tracker row is unchanged, so no second mail may appear.
replay_body="$(jq -nc --arg created "$created_at" --arg updated "$updated_at" \
  --arg url "$instance/$repository/issues/$issue_number" --arg repo "$repository" --arg login "$admin" \
  --argjson number "$issue_number" --argjson actor "$admin_id" \
  '{action:"opened",issue:{number:$number,html_url:$url,created_at:$created,updated_at:$updated},repository:{full_name:$repo,html_url:"http://gitea:3000/"+$repo},sender:{id:$actor,login:$login}}')"
signature="$(printf '%s' "$replay_body" | openssl dgst -sha256 -hmac "$(require_value GITEA_MAIL_BRIDGE_WEBHOOK_SECRET)" | awk '{print $NF}')"
replay_request="$(jq -nc --arg body "$replay_body" --arg signature "$signature" --arg delivery "smoke-replay-$issue_number" \
  '{body:$body,headers:{"X-Gitea-Event":"issues","X-Gitea-Delivery":$delivery,"X-Gitea-Signature":$signature}}')"
printf '%s' "$replay_request" | compose exec -T mcp-agent-mail python3 -c '
import json, sys, urllib.request
request = json.load(sys.stdin)
http = urllib.request.Request("http://gitea-mail-bridge:8080/v1/gitea/webhook", data=request["body"].encode(), headers=request["headers"])
with urllib.request.urlopen(http, timeout=30) as response:
    assert response.status == 202
'
wait_for_count "$mayor_mail" "$mayor_mail_token" "$thread_id" 1

# Let one full configured reconciliation cycle observe the same authoritative
# row before asserting that webhook replay plus polling still emitted once.
reconcile_seconds="$(value_for INTAKE_RECONCILE_SECONDS)"
reconcile_seconds="${reconcile_seconds:-30}"
sleep $((reconcile_seconds + 6))
wait_for_count "$mayor_mail" "$mayor_mail_token" "$thread_id" 1

labels="$(curl --fail --silent --show-error -H "Authorization: token $mayor_token" "$gitea_api/repos/$repository/labels?limit=100")"
managed_id="$(printf '%s' "$labels" | jq -er '.[] | select(.name == "gc:city-managed") | .id')"
approval_id="$(printf '%s' "$labels" | jq -er --arg label "$(require_value INTAKE_APPROVAL_LABEL)" '.[] | select(.name == $label) | .id')"
curl --fail --silent --show-error -H "Authorization: token $mayor_token" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/labels" \
  --data "$(jq -nc --argjson id "$managed_id" '{labels:[$id]}')" >/dev/null
revision="smoke-$issue_number"
plan_body="$(printf '### Gas City plan `%s`\n\nRun the disposable intake fixture.\n\n<!-- gascity:intake-plan:v1\n{"revision":"%s","repository":"%s"}\n-->' "$revision" "$revision" "$repository")"
curl --fail --silent --show-error -H "Authorization: token $mayor_token" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/comments" \
  --data "$(jq -nc --arg body "$plan_body" '{body:$body}')" >/dev/null
sleep 2
curl --fail --silent --show-error --user "$admin:$admin_password" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/labels" \
  --data "$(jq -nc --argjson id "$approval_id" '{labels:[$id]}')" >/dev/null

authorization="$(wait_for_authorization 'gc.intake.start-authorized.')"
printf '%s' "$authorization" | jq -e \
  '.type == "gc.intake.start-authorized.v1" and (.payload.pinned_base | test("^[0-9a-f]{40,64}$"))' >/dev/null

before_reply="$(tracker_count "$(fetch_inbox "$mayor_mail" "$mayor_mail_token")" "$thread_id")"
curl --fail --silent --show-error --user "$admin:$admin_password" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/comments" \
  --data '{"body":"External fixture reply"}' >/dev/null
wait_for_count "$mayor_mail" "$mayor_mail_token" "$thread_id" $((before_reply + 1))

before_city="$(tracker_count "$(fetch_inbox "$mayor_mail" "$mayor_mail_token")" "$thread_id")"
curl --fail --silent --show-error -H "Authorization: token $mayor_token" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/comments" \
  --data '{"body":"City fixture reply: must not loop to mail"}' >/dev/null
sleep 3
after_city="$(tracker_count "$(fetch_inbox "$mayor_mail" "$mayor_mail_token")" "$thread_id")"
test "$before_city" -eq "$after_city"

# Make one outbound send fail after its ledger transition, then restore Mail and
# restart the bridge. The durable outbox must recover exactly one delivery.
before_recovery="$after_city"
compose stop mcp-agent-mail >/dev/null
curl --silent --show-error --user "$admin:$admin_password" -H 'Content-Type: application/json' \
  -X POST "$gitea_api/repos/$repository/issues/$issue_number/comments" \
  --data '{"body":"External delayed fixture reply"}' >/dev/null || true
compose --profile mcp up -d --wait --wait-timeout 90 mcp-agent-mail
compose --profile gitea-mail-bridge restart gitea-mail-bridge >/dev/null
wait_for_count "$mayor_mail" "$mayor_mail_token" "$thread_id" $((before_recovery + 1))

# Simulate the private launcher. With the ledger directory read-only, the
# binding message must remain unacknowledged. Restoring it permits durable
# binding first and acknowledgement second.
authorization_id="$(printf '%s' "$authorization" | jq -er '.payload.id')"
topic="gc-binding-$(printf '%s' "$authorization_id" | sha256_hex | cut -c 1-32)"
binding="$(printf '%s' "$authorization" | jq -c --arg run "smoke-run-$issue_number" '
  {event_id:("gc.run.binding."+.payload.id),type:"gc.run.binding.v1",issue:.payload.issue,thread_id:.thread_id,
   payload:{issue:.payload.issue,plan:.payload.plan,authorization_id:.payload.id,pinned_base:.payload.pinned_base,run_id:$run}}')"
chmod 0500 state/gitea-mail-bridge
launcher_identity="$(require_value INTAKE_MAIL_LAUNCHER_IDENTITY)"
launcher_token="$(require_value MCP_AGENT_MAIL_LAUNCHER_REGISTRATION_TOKEN)"
mcp_call send_message "$(jq -nc --arg project "$(require_value MCP_AGENT_MAIL_PROJECT_KEY)" \
  --arg sender "$launcher_identity" --arg token "$launcher_token" --arg recipient "$(require_value INTAKE_MAIL_BRIDGE_IDENTITY)" \
  --arg subject "gc.run.binding.$authorization_id" --arg body "$binding" --arg thread "$thread_id" --arg topic "$topic" \
  '{project_key:$project,sender_name:$sender,sender_token:$token,to:[$recipient],subject:$subject,body_md:$body,thread_id:$thread,topic:$topic,ack_required:true}')" >/dev/null
compose --profile gitea-mail-bridge restart gitea-mail-bridge >/dev/null
sleep 3
binding_inbox="$(fetch_inbox "$(require_value INTAKE_MAIL_BRIDGE_IDENTITY)" "$(require_value MCP_AGENT_MAIL_BRIDGE_REGISTRATION_TOKEN)" "$topic")"
binding_message_id="$(printf '%s' "$binding_inbox" | jq -er --arg subject "gc.run.binding.$authorization_id" '.[] | select(.subject == $subject) | .id' | head -n 1)"
ack_before="$(compose exec -T mcp-agent-mail python3 -c 'import sqlite3,sys; db=sqlite3.connect("storage.sqlite3"); row=db.execute("select ack_ts from message_recipients where message_id=?",(int(sys.argv[1]),)).fetchone(); print("" if row is None or row[0] is None else row[0])' "$binding_message_id")"
test -z "$ack_before"
chmod 0700 state/gitea-mail-bridge
compose --profile gitea-mail-bridge restart gitea-mail-bridge >/dev/null
attempts=0
# Allow bridge startup plus two complete reconciliation intervals before
# declaring record-before-ack recovery absent.
while [ "$attempts" -lt 90 ]; do
  if jq -e --arg run "smoke-run-$issue_number" '[.records[].binding.run_id?] | index($run) != null' state/gitea-mail-bridge/ledger.json >/dev/null 2>&1; then
    ack_after="$(compose exec -T mcp-agent-mail python3 -c 'import sqlite3,sys; db=sqlite3.connect("storage.sqlite3"); row=db.execute("select ack_ts from message_recipients where message_id=?",(int(sys.argv[1]),)).fetchone(); print("" if row is None or row[0] is None else row[0])' "$binding_message_id")"
    if [ -n "$ack_after" ]; then
      break
    fi
  fi
  attempts=$((attempts + 1))
  sleep 1
done
test -n "${ack_after:-}"

curl --fail --silent --show-error --user "$admin:$admin_password" -H 'Content-Type: application/json' \
  -X PATCH "$gitea_api/repos/$repository/issues/$issue_number" --data '{"state":"closed"}' >/dev/null
completed_issue_number="$issue_number"
issue_number=
printf '%s\n' "PASS: City mail bridge fixture issue #$completed_issue_number covered ingress, replay, approval, suppression, restart, and binding durability"
