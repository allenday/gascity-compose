#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

curl --fail --silent --show-error http://127.0.0.1:3002/api/healthz >/dev/null
curl --fail --silent --show-error http://127.0.0.1:3000/api/health >/dev/null
curl --fail --silent --show-error http://127.0.0.1:9090/-/healthy >/dev/null
curl --fail --silent --show-error http://127.0.0.1:3100/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/endpoints/statuses >/dev/null

targets="$(curl --fail --silent --show-error http://127.0.0.1:9090/api/v1/targets)"
printf '%s' "$targets" | jq --exit-status '
  [.data.activeTargets[] | select(.labels.job == "otel-collector" or .labels.job == "node-exporter")]
  | length == 2 and all(.[]; .health == "up")
' >/dev/null

docker compose --env-file "$env_file" ps --status running >/dev/null
printf '%s\n' 'PASS: default platform endpoints and Prometheus targets are healthy'
