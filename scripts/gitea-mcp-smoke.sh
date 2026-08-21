#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

check_tools() {
  name="$1"
  port="$2"
  expected="$3"
  headers="$(mktemp -t gascity-mcp-headers.XXXXXX)"
  body="$(mktemp -t gascity-mcp-body.XXXXXX)"
  trap 'rm -f "$headers" "$body"' EXIT HUP INT TERM

  curl -fsS -D "$headers" -o "$body" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"gascity-compose-smoke","version":"1"}}}' \
    "http://127.0.0.1:${port}/mcp"
  session_id="$(awk 'tolower($1) == "mcp-session-id:" { print $2 }' "$headers" | tr -d '\r')"
  if [ -z "$session_id" ]; then
    printf '%s\n' "ERROR: $name did not return an MCP session id" >&2
    exit 1
  fi

  response="$(curl -fsS \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Mcp-Session-Id: $session_id" \
    --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    "http://127.0.0.1:${port}/mcp")"
  # Gitea MCP currently replies with application/json, but accept a compliant
  # SSE response too so the smoke remains valid across server versions.
  payload="$(printf '%s\n' "$response" | sed -n 's/^data: //p')"
  if [ -z "$payload" ]; then
    payload="$response"
  fi
  actual="$(printf '%s\n' "$payload" \
    | jq -r 'select(.id == 2) | .result.tools[] | .name' \
    | sort | paste -sd, -)"
  if [ "$actual" != "$expected" ]; then
    printf '%s\n' "ERROR: $name tools were $actual; expected $expected" >&2
    exit 1
  fi
  printf '%s\n' "PASS: $name exposes $actual"
  rm -f "$headers" "$body"
  trap - EXIT HUP INT TERM
}

check_tools mayor "$(value_for GITEA_MCP_MAYOR_PORT)" 'issue_read,issue_write,label_read'
check_tools observer "$(value_for GITEA_MCP_OBSERVER_PORT)" 'issue_read,label_read'
check_tools witness "$(value_for GITEA_MCP_WITNESS_PORT)" 'issue_read,issue_write,label_read'
