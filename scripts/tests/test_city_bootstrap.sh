#!/usr/bin/env sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
script="$root/scripts/city-bootstrap.sh"

require() {
  pattern="$1"
  if ! grep -Fq -- "$pattern" "$script"; then
    printf '%s\n' "missing bootstrap contract: $pattern" >&2
    exit 1
  fi
}

require 'gc rig add --city "$city_dir" --name "$rig_name" "$rig"'
require 'gc rig add --city "$city_dir" --name "$rig_name" --adopt "$rig"'
require 'gc rig list --city "$city_dir" --json'
require 'registered_rigs()'
require 'if rig_is_registered "$rig"; then'
require 'dolt config --global --add user.name'
require 'compose ps --status running --services city | rg -Fx city'
require 'ERROR: City is running; stop it before bootstrap'
require 'review_rig_dir="$(value GC_CITY_DOCS_REVIEW_RIG_DIR)"'
require '--name "$city_name" "$city_dir"'
require "jq -e --arg rig \"\$rig\" '.rigs[] | select(.path == \$rig)'"
require 'ERROR: rig registration missing from city config'
# The City image runs with HOST_UID:GID and its GC_HOME bind mount must be
# writable before this bootstrap invokes `gc` in that container.
require 'city_uid="$(value HOST_UID)"; city_uid="${city_uid:-1000}"'
require 'city_gid="$(value HOST_GID)"; city_gid="${city_gid:-1000}"'
require 'mkdir -p state/gc-runtime'
require 'chown -R "$city_uid:$city_gid" state/gc-runtime'
require 'chmod 0700 state/gc-runtime'

if grep -Fq '|| true' "$script"; then
  printf '%s\n' 'city bootstrap must not hide rig registration failures' >&2
  exit 1
fi

runtime_chown_line="$(grep -n 'chown -R "\$city_uid:\$city_gid" state/gc-runtime' "$script" | head -n 1 | cut -d: -f1)"
first_gc_line="$(grep -n '^gc() {' "$script" | head -n 1 | cut -d: -f1)"
if [ -z "$runtime_chown_line" ] || [ -z "$first_gc_line" ] || [ "$runtime_chown_line" -ge "$first_gc_line" ]; then
  printf '%s\n' 'City runtime state ownership must be prepared before bootstrap invokes gc' >&2
  exit 1
fi

# Exercise a fresh host directory through the real bootstrap script. The
# lightweight Compose stub only supplies the already-registered rig response;
# all filesystem ownership work is performed by the script under test.
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/bin" "$fixture/city" "$fixture/rig"
: > "$fixture/city/city.toml"
cat > "$fixture/bin/docker" <<'EOF'
#!/bin/sh
set -eu
shift
if [ "$1" = "--env-file" ]; then
  shift 2
fi
case "$1" in
  ps) exit 0 ;;
  run) printf '%s\n' "{\"rigs\":[{\"path\":\"$TEST_CITY_BOOTSTRAP_RIG\"}]}" ;;
  *) exit 1 ;;
esac
EOF
chmod 0755 "$fixture/bin/docker"
cat > "$fixture/bin/rg" <<'EOF'
#!/bin/sh
# The fixture has no running City service.
exit 1
EOF
chmod 0755 "$fixture/bin/rg"
cat > "$fixture/city.env" <<EOF
CITY_DIR=$fixture/city
MY_PROJECT_DIR=$fixture/rig
GC_CITY_DOCS_REVIEW_RIG_DIR=$fixture/rig
CITY_NAME=fixture-city
HOST_UID=$(id -u)
HOST_GID=$(id -g)
EOF
(
  cd "$fixture"
  PATH="$fixture/bin:$PATH" TEST_CITY_BOOTSTRAP_RIG="$fixture/rig" ENV_FILE="$fixture/city.env" sh "$script" >/dev/null
)
[ "$(stat -c '%u:%g:%a' "$fixture/state/gc-runtime")" = "$(id -u):$(id -g):700" ] || {
  printf '%s\n' 'City bootstrap did not provision a private HOST_UID:GID runtime directory' >&2
  exit 1
}

printf '%s\n' 'PASS: city bootstrap fails closed on rig registration'
