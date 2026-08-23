#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

value_for() {
  key="$1"
  fallback="$2"
  value="$(awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file")"
  printf '%s' "${value:-$fallback}"
}

gitea_port="$(value_for GITEA_HTTP_PORT 3002)"
grafana_port="$(value_for GRAFANA_PORT 3000)"
prometheus_port="$(value_for PROMETHEUS_PORT 9090)"
loki_port="$(value_for LOKI_PORT 3100)"
gatus_port="$(value_for GATUS_PORT 8080)"

curl --fail --silent --show-error "http://127.0.0.1:${gitea_port}/api/healthz" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1:${grafana_port}/api/health" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1:${prometheus_port}/-/healthy" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1:${loki_port}/ready" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1:${gatus_port}/api/v1/endpoints/statuses" >/dev/null

targets="$(curl --fail --silent --show-error "http://127.0.0.1:${prometheus_port}/api/v1/targets")"
printf '%s' "$targets" | jq --exit-status '
  [.data.activeTargets[] | select(.labels.job == "otel-collector" or .labels.job == "node-exporter")]
  | length == 2 and all(.[]; .health == "up")
' >/dev/null

docker compose --env-file "$env_file" ps --status running >/dev/null
printf '%s\n' 'PASS: default platform endpoints and Prometheus targets are healthy'
