#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"

if [ ! -r "$env_file" ]; then
  printf '%s\n' "ERROR: cannot read $env_file; copy .env.example to .env first" >&2
  exit 1
fi

value_for() {
  key="$1"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"
}

upsert() {
  key="$1"
  value="$2"
  tmp="${env_file}.tmp.$$"
  umask 077
  awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; seen = 1; next }
    { print }
    END { if (!seen) print key "=" value }
  ' "$env_file" >"$tmp"
  mv "$tmp" "$env_file"
}

compose() {
  docker compose --env-file "$env_file" "$@"
}

gitea_port="$(value_for GITEA_HTTP_PORT)"
gitea_port="${gitea_port:-3002}"
fixture_user="${WOODPECKER_FIXTURE_USER:-woodpecker-fixture}"
fixture_repo="$(value_for WOODPECKER_FIXTURE_REPOSITORY)"
fixture_repo="${fixture_repo:-gascity-compose-fixture}"
woodpecker_host="$(value_for WOODPECKER_HOST)"
woodpecker_host="${woodpecker_host:-http://127.0.0.1:8000}"

compose up -d --wait --wait-timeout 90 gitea

# This regular account owns only the private fixture repository. The Gitea
# administrator creates it locally, but its token never receives admin scope.
fixture_password="$(value_for GITEA_WOODPECKER_PASSWORD)"
if [ -z "$fixture_password" ]; then
  fixture_password="$(openssl rand -base64 36 | tr -d '\n')"
  upsert GITEA_WOODPECKER_PASSWORD "$fixture_password"
fi
compose exec -T gitea gitea admin user create \
  --username "$fixture_user" \
  --password "$fixture_password" \
  --email "${fixture_user}@localhost" \
  --must-change-password=false >/dev/null 2>&1 || true

fixture_token="$(value_for GITEA_WOODPECKER_TOKEN)"
if [ -z "$fixture_token" ]; then
  fixture_token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$fixture_user" \
    --token-name woodpecker-fixture \
    --scopes write:repository,write:user | awk -F': ' 'END { print $NF }')"
  test -n "$fixture_token"
  upsert GITEA_WOODPECKER_TOKEN "$fixture_token"
fi

package_token="$(value_for GITEA_WOODPECKER_PACKAGE_TOKEN)"
if [ -z "$package_token" ]; then
  package_token="$(compose exec -T gitea gitea admin user generate-access-token \
    --username "$fixture_user" \
    --token-name woodpecker-fixture-package \
    --scopes write:package | awk -F': ' 'END { print $NF }')"
  test -n "$package_token"
  upsert GITEA_WOODPECKER_PACKAGE_TOKEN "$package_token"
fi

gitea_api="http://127.0.0.1:${gitea_port}/api/v1"
oauth_client="$(value_for WOODPECKER_GITEA_CLIENT)"
oauth_secret="$(value_for WOODPECKER_GITEA_SECRET)"
if [ -z "$oauth_client" ] || [ -z "$oauth_secret" ]; then
  oauth="$(curl --fail --silent --show-error \
    -H "Authorization: token $fixture_token" \
    -H 'Content-Type: application/json' \
    -X POST "$gitea_api/user/applications/oauth2" \
    --data "$(jq -nc --arg name gascity-compose-woodpecker --arg redirect "${woodpecker_host}/authorize" '{name:$name,redirect_uris:[$redirect]}')")"
  oauth_client="$(printf '%s' "$oauth" | jq -er '.client_id')"
  oauth_secret="$(printf '%s' "$oauth" | jq -er '.client_secret')"
  upsert WOODPECKER_GITEA_CLIENT "$oauth_client"
  upsert WOODPECKER_GITEA_SECRET "$oauth_secret"
fi

if ! curl --fail --silent --show-error -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}" >/dev/null 2>&1; then
  curl --fail --silent --show-error \
    -H "Authorization: token $fixture_token" \
    -H 'Content-Type: application/json' \
    -X POST "$gitea_api/user/repos" \
    --data "$(jq -nc --arg name "$fixture_repo" '{name:$name,private:true,auto_init:true,default_branch:"main"}')" >/dev/null
fi

fixture_pipeline() {
  cat <<'PIPELINE_EOF'
when:
  - event: [push, manual]
steps:
  fixture:
    image: alpine:3.20
    commands:
      - test -f README.md
      - apk add --no-cache curl
      - artifact="fixture-$CI_COMMIT_SHA.txt"
      - printf "fixture artifact for %s\\n" "$CI_COMMIT_SHA" > "$artifact"
      - curl --fail --user "woodpecker-fixture:$GITEA_FIXTURE_PACKAGE_TOKEN" --upload-file "$artifact" "http://gitea:3000/api/packages/woodpecker-fixture/generic/gascity-compose-fixture/$CI_COMMIT_SHA/$artifact"
      - 'printf "run URL: %s\\n" "$CI_PIPELINE_URL"'
      - 'printf "artifact URL: %s/api/packages/woodpecker-fixture/generic/gascity-compose-fixture/%s/%s\\n" "$GITEA_FIXTURE_PUBLIC_URL" "$CI_COMMIT_SHA" "$artifact"'
PIPELINE_EOF
}
pipeline="$(fixture_pipeline)"
encoded_pipeline="$(printf '%s' "$pipeline" | base64 | tr -d '\n')"
existing_pipeline="$(curl --silent --show-error -H "Authorization: token $fixture_token" \
  "$gitea_api/repos/${fixture_user}/${fixture_repo}/contents/.woodpecker.yml" 2>/dev/null || true)"
existing_content="$(printf '%s' "$existing_pipeline" | jq -r '.content // empty' | tr -d '\n')"
existing_sha="$(printf '%s' "$existing_pipeline" | jq -r '.sha // empty')"
if [ "$existing_content" != "$encoded_pipeline" ]; then
  if [ -n "$existing_sha" ]; then
    curl --fail --silent --show-error \
      -H "Authorization: token $fixture_token" \
      -H 'Content-Type: application/json' \
      -X PUT "$gitea_api/repos/${fixture_user}/${fixture_repo}/contents/.woodpecker.yml" \
      --data "$(jq -nc --arg content "$encoded_pipeline" --arg sha "$existing_sha" '{content:$content,message:"Reconcile Woodpecker fixture pipeline",branch:"main",sha:$sha}')" >/dev/null
  else
  curl --fail --silent --show-error \
    -H "Authorization: token $fixture_token" \
    -H 'Content-Type: application/json' \
    -X POST "$gitea_api/repos/${fixture_user}/${fixture_repo}/contents/.woodpecker.yml" \
      --data "$(jq -nc --arg content "$encoded_pipeline" '{content:$content,message:"Add Woodpecker fixture pipeline",branch:"main"}')" >/dev/null
  fi
fi

if [ -z "$(value_for WOODPECKER_AGENT_SECRET)" ]; then
  printf '%s\n' 'ERROR: set WOODPECKER_AGENT_SECRET to a high-entropy value, then rerun this target' >&2
  exit 1
fi

printf '%s\n' "PASS: private Gitea fixture ${fixture_user}/${fixture_repo} and Woodpecker OAuth credentials are ready"
