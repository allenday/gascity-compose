#!/bin/sh
set -eu

: "${CITY_PATH:?CITY_PATH must be the in-container path to the mounted city}"
: "${CITY_NAME:?CITY_NAME must be set}"
: "${GC_HOME:?GC_HOME must be set}"

mkdir -p "$GC_HOME"
mkdir -p "$HOME"
mkdir -p "${CODEX_HOME:-/run/codex}"
cp /run/secrets/codex-auth.json "${CODEX_HOME:-/run/codex}/auth.json"
chmod 0600 "${CODEX_HOME:-/run/codex}/auth.json"
for template in /run/secrets/codex-config/*.toml.template; do
  output="${CODEX_HOME:-/run/codex}/$(basename "${template%.template}")"
  envsubst '$TAILNET_OLLAMA_BASE_URL $RUNPOD_OPENAI_BASE_URL $GEMMA_MODEL' \
    < "$template" > "$output"
done
chmod 0600 "${CODEX_HOME:-/run/codex}"/*.toml
cat > "$GC_HOME/cities.toml" <<EOF
[[cities]]
path = "${CITY_PATH}"
name = "${CITY_NAME}"
EOF

# Keep the API private to the host at the Compose port mapping, while allowing
# other services on this Compose network (role-health and Gatus) to reach it.
cat > "$GC_HOME/supervisor.toml" <<EOF
[supervisor]
bind = "0.0.0.0"
port = 8372
allowed_hosts = ["city", "localhost", "127.0.0.1"]
EOF

if ! dolt config --global --get user.name >/dev/null 2>&1; then
  dolt config --global --add user.name "${DOLT_USER_NAME:-Allen Day}"
fi
if ! dolt config --global --get user.email >/dev/null 2>&1; then
  dolt config --global --add user.email "${DOLT_USER_EMAIL:-allenday@allenday.com}"
fi

# The city lockfile pins remote packs but the controller cache lives in the
# Compose runtime mount, not in the source checkout. Populate it on every
# start; gc import install is idempotent when the lock is already cached.
gc --city "$CITY_PATH" import install

exec gc supervisor run
