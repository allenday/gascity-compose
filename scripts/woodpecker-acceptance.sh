#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

require_value() {
  key="$1"
  value="$(value_for "$key")"
  if [ -z "$value" ]; then
    printf '%s\n' "ERROR: set $key in $env_file" >&2
    exit 1
  fi
  printf '%s' "$value"
}

gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
fixture_user="${WOODPECKER_FIXTURE_USER:-woodpecker-fixture}"
fixture_repo="$(value_for WOODPECKER_FIXTURE_REPOSITORY)"
fixture_repo="${fixture_repo:-gascity-compose-fixture}"
fixture_token="$(require_value GITEA_WOODPECKER_TOKEN)"
package_token="$(require_value GITEA_WOODPECKER_PACKAGE_TOKEN)"
gitea_api="http://127.0.0.1:${gitea_port}/api/v1"
package_api="http://127.0.0.1:${gitea_port}/api/packages"

# Repository activation creates this Woodpecker-owned callback. A successful
# delivery proves Gitea can reach the internal webhook URL from its container.
hook_id="$(curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/hooks" | \
  jq -er '[.[] | select(.config.url | startswith("http://woodpecker-server:8000"))] | last.id')"
curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/hooks/${hook_id}/deliveries" | \
  jq -e 'any(.[]; .response.status >= 200 and .response.status < 300)' >/dev/null

# The fixture uses the commit SHA as the immutable package version. Download
# the retained file through the loopback-published package registry.
artifact_version="$(curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/packages/${fixture_user}?type=generic" | \
  jq -er '[.[] | select(.name == "gascity-compose-fixture")] | sort_by(.created_at) | last.version')"
case "$artifact_version" in
  ''|*[!0-9a-f]*)
    printf '%s\n' "ERROR: artifact version is not an immutable commit SHA: $artifact_version" >&2
    exit 1
    ;;
esac
if [ "${#artifact_version}" -ne 40 ]; then
  printf '%s\n' "ERROR: artifact version is not an immutable commit SHA: $artifact_version" >&2
  exit 1
fi
artifact="fixture-${artifact_version}.txt"
artifact_url="${package_api}/${fixture_user}/generic/gascity-compose-fixture/${artifact_version}/${artifact}"
curl --fail --silent --show-error --user "${fixture_user}:${package_token}" "$artifact_url" | \
  grep -Fx "fixture artifact for ${artifact_version}" >/dev/null

printf '%s\n' "PASS: delivered Woodpecker webhook and retained immutable artifact ${artifact_url}"
