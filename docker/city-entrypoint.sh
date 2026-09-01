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

# Superpowers is the opinionated build methodology for this deployment. Its
# pack vendors provider-neutral skills which Gas City materializes for each
# lane, so Codex and OpenAI-compatible agents receive the same methodology.
# Existing cities remain free to manage or replace an already-declared binding.
if [ "${SUPERPOWERS_PACK_ENABLED:-true}" = "true" ] && \
   ! grep -Eq '^[[:space:]]*\[imports\.superpowers\][[:space:]]*$' "$CITY_PATH/pack.toml"; then
  gc --city "$CITY_PATH" import add \
    "${SUPERPOWERS_PACK_SOURCE:-https://github.com/gastownhall/gascity-packs/tree/main/superpowers}" \
    --name superpowers \
    --version "${SUPERPOWERS_PACK_VERSION:-sha:3b3b89f2011e06d84459aa7bea1552382f13930a}"
fi

# The docs-impact agent definition and skill come from the exact read-only pack
# checkout used by the intake services. Only the trusted City imports it; the
# public webhook and networkless evidence worker never receive Codex auth.
if [ "${GC_CITY_DOCS_REVIEW_ENABLED:-true}" = "true" ] && \
   ! grep -Eq '^[[:space:]]*\[imports\.github-docs-impact\][[:space:]]*$' "$CITY_PATH/pack.toml"; then
  gc --city "$CITY_PATH" import add /opt/gascity-packs/github --name github-docs-impact
fi

# The imported agent deliberately has no provider of its own. Bind it to this
# fixed, credential-free Codex lane exactly once, so a dispatch produces a
# real reviewer session instead of merely recording a routed bead.
if [ "${GC_CITY_DOCS_REVIEW_ENABLED:-true}" = "true" ]; then
  compose_dir="$CITY_PATH/.gc/compose"
  docs_fragment="$compose_dir/city-docs-impact.toml"
  mkdir -p "$compose_dir"
  cp /usr/local/share/gascity-compose/city-docs-impact.toml "$docs_fragment"
  if ! grep -Fq '.gc/compose/city-docs-impact.toml' "$CITY_PATH/city.toml"; then
    if grep -Eq '^[[:space:]]*include[[:space:]]*=' "$CITY_PATH/city.toml"; then
      sed -i '0,/^[[:space:]]*include[[:space:]]*=/{s#]$#, ".gc/compose/city-docs-impact.toml"]#}' "$CITY_PATH/city.toml"
    else
      sed -i '1i include = [".gc/compose/city-docs-impact.toml"]' "$CITY_PATH/city.toml"
    fi
  fi
fi

# The city lockfile pins remote packs but the controller cache lives in the
# Compose runtime mount, not in the source checkout. Populate it on every
# start; gc import install is idempotent when the lock is already cached.
gc --city "$CITY_PATH" import install

if [ "${CITY_MAIL_WAKE_ENABLED:-true}" = "true" ]; then
  /usr/local/bin/city-mail-wake &
fi

if [ "${CITY_MAIL_LOCAL_LAUNCH_ENABLED:-true}" = "true" ]; then
  /usr/local/bin/city-mail-local-launch &
fi

# The GitHub runtime records a pending dispatch in shared durable state.  Only
# this process is allowed to sling it: it has the live supervisor connection,
# unlike the credentialed GitHub services.
if [ "${GC_CITY_DOCS_REVIEW_ENABLED:-true}" = "true" ]; then
  /usr/local/bin/github-docs-impact-city-dispatcher &
fi

exec gc supervisor run
