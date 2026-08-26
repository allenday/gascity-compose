#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file; copy .env.example to .env first" >&2
  exit 1
fi

format=text
while [ "$#" -gt 0 ]; do
  case "$1" in
    --format)
      shift
      [ "$#" -gt 0 ] || { printf '%s\n' 'ERROR: --format requires a value' >&2; exit 1; }
      format="$1"
      ;;
    *)
      printf '%s\n' "ERROR: unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

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

manifest_path="$(value_for INTAKE_MANIFEST_PATH)"
manifest_path="${manifest_path:-./config/gitea-intake.toml}"
case "$manifest_path" in
  /*) manifest_file="$manifest_path" ;;
  *) manifest_file="$(dirname "$env_file")/$manifest_path" ;;
esac
if [ ! -r "$manifest_file" ]; then
  printf '%s\n' "ERROR: cannot read intake manifest $manifest_file" >&2
  exit 1
fi

derived_env="$(python3 - "$manifest_file" <<'PY'
import re
import sys
import tomllib

MAYOR = "gascity-mcp-mayor"
BRIDGE = "gascity-mail-bridge"
LAUNCHER = "gascity-mail-launcher"

manifest_path = sys.argv[1]
with open(manifest_path, "rb") as handle:
    data = tomllib.load(handle)

minimum_role = data.get("minimum_repository_role")
if minimum_role != "triage":
    raise SystemExit("ERROR: intake manifest minimum_repository_role must be \"triage\"")

repositories = data.get("repositories")
if not isinstance(repositories, list) or not repositories:
    raise SystemExit("ERROR: intake manifest repositories must be a non-empty array")

pattern = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
seen = set()
normalized = []
for raw in repositories:
    if not isinstance(raw, str) or not pattern.match(raw):
        raise SystemExit(f"ERROR: intake manifest repository must be exact owner/repo: {raw!r}")
    if raw in seen:
        raise SystemExit(f"ERROR: intake manifest repository is duplicated: {raw}")
    seen.add(raw)
    normalized.append(raw)

print(f"INTAKE_ACCOUNT={MAYOR}")
print(f"INTAKE_REPOSITORY_SCOPES={','.join(normalized)}")
print(f"INTAKE_CITY_IDENTITIES={MAYOR},{BRIDGE},{LAUNCHER}")
print("INTAKE_MINIMUM_REPOSITORY_ROLE=triage")
PY
)"

get_derived() {
  key="$1"
  printf '%s\n' "$derived_env" | awk -F= -v key="$key" '$1 == key { print substr($0, length(key) + 2); exit }'
}

intake_account="$(get_derived INTAKE_ACCOUNT)"
repository_scopes="$(get_derived INTAKE_REPOSITORY_SCOPES)"
city_identities="$(get_derived INTAKE_CITY_IDENTITIES)"
minimum_role="$(get_derived INTAKE_MINIMUM_REPOSITORY_ROLE)"

stack_username="$(require_value STACK_USERNAME)"
admin_token="$(value_for GITEA_MAIL_BRIDGE_ADMIN_TOKEN)"
stack_password="$(value_for STACK_PASSWORD)"
if [ -z "$admin_token" ] && [ -z "$stack_password" ]; then
  printf '%s\n' 'ERROR: set GITEA_MAIL_BRIDGE_ADMIN_TOKEN or STACK_PASSWORD before running the intake doctor' >&2
  exit 1
fi

gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
api_root="${GITEA_API_ROOT:-http://gitea:3000/api/v1}"
curl_bin="${CURL_BIN:-curl}"

curl_gitea() {
  set -- --fail --silent --show-error "$@"
  case "$api_root" in
    http://gitea:3000/*|https://gitea:3000/*)
      set -- --connect-to "gitea:3000:127.0.0.1:${gitea_port}" "$@"
      ;;
  esac
  if [ -n "$admin_token" ]; then
    "$curl_bin" -H "Authorization: token $admin_token" "$@"
  else
    "$curl_bin" --user "$stack_username:$stack_password" "$@"
  fi
}

if ! operator_user="$(curl_gitea "$api_root/user")"; then
  printf '%s\n' 'ERROR: intake doctor could not authenticate to the Gitea API' >&2
  exit 1
fi
printf '%s' "$operator_user" | jq -e --arg login "$stack_username" '.login == $login and .is_admin == true' >/dev/null || {
  printf '%s\n' "ERROR: Gitea operator $stack_username must be an admin user" >&2
  exit 1
}

for repository in $(printf '%s' "$repository_scopes" | tr ',' ' '); do
  repo_json="$(curl_gitea "$api_root/repos/$repository")" || {
    printf '%s\n' "ERROR: intake repository $repository must exist and be readable" >&2
    exit 1
  }
  printf '%s' "$repo_json" | jq -e '.has_issues == true' >/dev/null || {
    printf '%s\n' "ERROR: intake repository $repository must have issues enabled" >&2
    exit 1
  }
  printf '%s' "$repo_json" | jq -e '.private == true or .internal == true' >/dev/null || {
    printf '%s\n' "ERROR: intake repository $repository must be private or internal" >&2
    exit 1
  }
  issues_json="$(curl_gitea "$api_root/repos/$repository/issues?state=all&limit=1")" || {
    printf '%s\n' "ERROR: intake repository $repository issue history could not be inspected" >&2
    exit 1
  }
  issue_count="$(printf '%s' "$issues_json" | jq -er 'length')"
  if [ "$issue_count" -ne 0 ]; then
    printf '%s\n' "ERROR: intake repository $repository must have no existing issues before onboarding" >&2
    exit 1
  fi
done

for identity in "$intake_account" gascity-mail-bridge gascity-mail-launcher; do
  if account_json="$(curl_gitea "$api_root/users/$identity" 2>/dev/null)"; then
    printf '%s' "$account_json" | jq -e --arg login "$identity" '.login == $login and .restricted == true and .is_admin == false' >/dev/null || {
      printf '%s\n' "ERROR: fixed role account $identity must be restricted and non-admin" >&2
      exit 1
    }
  else
    status=$?
    if [ "$status" -ne 22 ]; then
      printf '%s\n' "ERROR: fixed role account $identity could not be inspected" >&2
      exit 1
    fi
  fi
done

case "$format" in
  env)
    printf '%s\n' "$derived_env"
    ;;
  text)
    printf '%s\n' 'PASS: Gitea intake manifest is valid and onboarding preflight passed'
    printf '%s\n' "intake repositories: $repository_scopes"
    printf '%s\n' "fixed city identities: $city_identities"
    printf '%s\n' "minimum repository role: $minimum_role"
    ;;
  *)
    printf '%s\n' "ERROR: unsupported format $format" >&2
    exit 1
    ;;
esac
