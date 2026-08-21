#!/bin/sh
set -eu

: "${CITY_PATH:?CITY_PATH must be the in-container path to the mounted city}"
: "${CITY_NAME:?CITY_NAME must be set}"
: "${GC_HOME:?GC_HOME must be set}"

mkdir -p "$GC_HOME"
cat > "$GC_HOME/cities.toml" <<EOF
[[cities]]
path = "${CITY_PATH}"
name = "${CITY_NAME}"
EOF

exec gc supervisor run
