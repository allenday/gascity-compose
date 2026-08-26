#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
value() { awk -F= -v key="$1" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"; }
required() { result="$(value "$1")"; test -n "$result"; printf '%s' "$result"; }
compose() { docker compose --env-file "$env_file" "$@"; }
admin="$(required STACK_USERNAME)"; token="$(required GITEA_MAIL_BRIDGE_ADMIN_TOKEN)"
repository="$admin/$(required GITEA_MAIL_BRIDGE_FIXTURE_REPOSITORY)"; port="$(value GITEA_HTTP_PORT)"; port="${port:-3002}"; api="http://127.0.0.1:${port}/api/v1"; issue_number=""
cleanup() { if [ -n "$issue_number" ]; then curl --silent --user "$admin:$token" -H 'Content-Type: application/json' -X PATCH "$api/repos/$repository/issues/$issue_number" --data '{"state":"closed"}' >/dev/null || true; fi; }
trap cleanup EXIT
make gitea-mail-launcher-up ENV_FILE="$env_file"
mayor="$(required INTAKE_ACCOUNT)"; mayor_token="$(required GITEA_MAYOR_TOKEN)"
issue="$(curl --fail --silent --user "$admin:$token" -H 'Content-Type: application/json' -X POST "$api/repos/$repository/issues" --data "$(jq -nc --arg mayor "$mayor" '{title:"Real City launcher fixture",body:"Disposable Gate D launcher fixture",assignees:[$mayor]}')")"; issue_number="$(printf '%s' "$issue" | jq -er '.number')"
labels="$(curl --fail --silent -H "Authorization: token $mayor_token" "$api/repos/$repository/labels?limit=100")"; managed="$(printf '%s' "$labels" | jq -er '.[] | select(.name == "gc:city-managed") | .id')"; approved="$(printf '%s' "$labels" | jq -er --arg name "$(required INTAKE_APPROVAL_LABEL)" '.[] | select(.name == $name) | .id')"
curl --fail --silent -H "Authorization: token $mayor_token" -H 'Content-Type: application/json' -X POST "$api/repos/$repository/issues/$issue_number/labels" --data "$(jq -nc --argjson id "$managed" '{labels:[$id]}')" >/dev/null
revision="launcher-$issue_number"; body="$(printf '### Gas City plan `%s`\n\nLaunch the real City acceptance workflow.\n\n<!-- gascity:intake-plan:v1\n{"revision":"%s","repository":"%s"}\n-->' "$revision" "$revision" "$repository")"
curl --fail --silent -H "Authorization: token $mayor_token" -H 'Content-Type: application/json' -X POST "$api/repos/$repository/issues/$issue_number/comments" --data "$(jq -nc --arg body "$body" '{body:$body}')" >/dev/null; sleep 2
curl --fail --silent --user "$admin:$token" -H 'Content-Type: application/json' -X POST "$api/repos/$repository/issues/$issue_number/labels" --data "$(jq -nc --argjson id "$approved" '{labels:[$id]}')" >/dev/null
attempts=0
while [ "$attempts" -lt 180 ]; do
  record="$(jq -c --argjson issue "$issue_number" '[.records | to_entries[] | .value | select(.issue.number == $issue)] | first // empty' state/gitea-mail-bridge/ledger.json 2>/dev/null || true)"
  if [ -n "$record" ] && printf '%s' "$record" | jq -e '(.authorization.id | length > 0) and (.binding.run_id | length > 0) and (.binding.run_id | startswith("smoke-run-") | not) and (.binding.authorization_id == .authorization.id) and (.binding.pinned_base == .authorization.pinned_base)' >/dev/null 2>&1; then
    printf '%s\n' "PASS: real City launcher fixture issue #$issue_number produced immutable City run binding"; exit 0
  fi
  attempts=$((attempts + 1)); sleep 1
done
printf '%s\n' "ERROR: launcher did not bind a real City run for issue #$issue_number" >&2; exit 1
