#!/bin/sh
# Validate the local inputs and route contract for the GitHub docs-impact profile.
# It performs no GitHub mutation and creates no review work.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
env_file=${ENV_FILE:-"$root/.env"}

fail() {
  echo "github-docs-impact preflight: $*" >&2
  exit 1
}

[ -f "$env_file" ] || fail "environment file not found: $env_file"

value() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$env_file"
}

require_value() {
  key=$1
  result=$(value "$key")
  [ -n "$result" ] || fail "$key is required in $env_file"
  printf '%s' "$result"
}

city_dir=$(require_value CITY_DIR)
pack_dir=$(require_value GITHUB_PACK_DIR)
app_id=$(value GITHUB_APP_ID)
webhook_secret=$(value GITHUB_WEBHOOK_SECRET)
private_key=$(value GITHUB_APP_PRIVATE_KEY_PEM)
state_root=$(value GITHUB_INTAKE_STATE_ROOT)
state_root=${state_root:-"$root/state/github-intake"}

case "$city_dir" in
  /*) ;;
  *) fail "CITY_DIR must be an absolute path" ;;
esac
[ -d "$city_dir" ] || fail "CITY_DIR does not exist: $city_dir"

case "$pack_dir" in
  /*) ;;
  *) fail "GITHUB_PACK_DIR must be an absolute path" ;;
esac
[ -d "$pack_dir" ] || fail "GITHUB_PACK_DIR does not exist: $pack_dir"
for required in github/scripts/github_intake_docs_review_runtime.py github/scripts/github_intake_docs_impact.py github/scripts/github_intake_docs_journey_commands.py github/agents/docs-impact-reviewer/prompt.template.md github/agents/docs-journey/prompt.template.md; do
  [ -f "$pack_dir/$required" ] || fail "GITHUB_PACK_DIR lacks $required"
done

app_from_env=1
case "$app_id" in *[!0-9]*|'') app_from_env=0 ;; esac
[ -n "$webhook_secret" ] || app_from_env=0
[ -n "$private_key" ] || app_from_env=0
if [ "$app_from_env" -ne 1 ]; then
  config_file="$state_root/data/config.json"
  python3 - "$config_file" <<'PY' || fail "GitHub App credentials must be in .env or $config_file"
import json
import sys

try:
    app = json.load(open(sys.argv[1], encoding="utf-8")).get("app", {})
except (OSError, ValueError):
    raise SystemExit(1)
required = ("app_id", "webhook_secret", "private_key_pem")
raise SystemExit(not all(str(app.get(key, "")).strip() for key in required))
PY
fi
ENV_FILE="$env_file" docker compose --env-file "$env_file" --profile github-docs-impact config --quiet
printf '%s\n' 'github-docs-impact preflight: ready'
