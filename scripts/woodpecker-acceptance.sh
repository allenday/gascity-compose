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
woodpecker_host="$(value_for WOODPECKER_HOST)"
woodpecker_host="${woodpecker_host:-http://127.0.0.1:8000}"
gitea_api="http://127.0.0.1:${gitea_port}/api/v1"
package_api="http://127.0.0.1:${gitea_port}/api/packages"

# Repository activation creates this Woodpecker-owned callback. Gitea 1.27
# exposes hook registration but not delivery history via REST, so correlate the
# registered callback with Woodpecker's successful status on the branch head.
hook_id="$(curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/hooks" | \
  jq -er '[.[] | select(.config.url | startswith("http://woodpecker-server:8000"))] | last.id')"
test -n "$hook_id"
commit_sha="$(curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/branches/main" | jq -er '.commit.id')"
case "$commit_sha" in ''|*[!0-9a-f]*) exit 1;; esac
if [ "${#commit_sha}" -ne 40 ]; then exit 1; fi
run_url="$(curl --fail --silent --show-error \
  -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/statuses/$commit_sha" | \
  jq -er --arg host "$woodpecker_host" '[.[] | select(.status == "success" and (.target_url | startswith($host))) | .target_url] | first')"

# The fixture uses the commit SHA as the immutable package version. Download
# the retained file through the loopback-published package registry.
artifact_version="$commit_sha"
artifact="fixture-${artifact_version}.txt"
artifact_url="${package_api}/${fixture_user}/generic/gascity-compose-fixture/${artifact_version}/${artifact}"
curl --fail --silent --show-error --user "${fixture_user}:${package_token}" "$artifact_url" | \
  grep -Fx "fixture artifact for ${artifact_version}" >/dev/null

printf '%s\n' "PASS: registered webhook ${hook_id}, ${run_url} handled commit ${commit_sha}, and retained artifact ${artifact_url}"
