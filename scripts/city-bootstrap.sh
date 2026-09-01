#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
value() { awk -F= -v key="$1" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"; }
city_dir="$(value CITY_DIR)"
project_dir="$(value MY_PROJECT_DIR)"
review_rig_dir="$(value GC_CITY_DOCS_REVIEW_RIG_DIR)"
city_name="$(value CITY_NAME)"
[ -n "$city_name" ] || city_name=my-city
for path in "$city_dir" "$project_dir" "$review_rig_dir"; do
  case "$path" in /*) ;; *) echo "ERROR: absolute path required: $path" >&2; exit 1;; esac
done
compose() { docker compose --env-file "$env_file" "$@"; }
if compose ps --status running --services city | rg -Fx city >/dev/null; then
  printf '%s\n' 'ERROR: City is running; stop it before bootstrap to preserve the single-supervisor invariant.' >&2
  exit 1
fi

# Compose runs City and its two docs-impact helpers as HOST_UID:GID. Docker
# otherwise creates this ignored bind mount as root on a fresh host, which
# prevents the first unprivileged City bootstrap from writing GC_HOME.
city_uid="$(value HOST_UID)"; city_uid="${city_uid:-1000}"
city_gid="$(value HOST_GID)"; city_gid="${city_gid:-1000}"
case "$city_uid:$city_gid" in
  *[!0-9:]* | :* | *:) printf '%s\n' 'ERROR: HOST_UID and HOST_GID must be numeric when set' >&2; exit 1 ;;
esac
mkdir -p state/gc-runtime
chown -R "$city_uid:$city_gid" state/gc-runtime
chmod 0700 state/gc-runtime

gc() {
  compose run --rm --no-deps --entrypoint sh city -ec '
    dolt config --global --add user.name "${DOLT_USER_NAME:-Allen Day}"
    dolt config --global --add user.email "${DOLT_USER_EMAIL:-allenday@allenday.com}"
    exec gc "$@"
  ' sh "$@"
}

if [ ! -f "$city_dir/city.toml" ]; then
  mkdir -p "$city_dir"
  gc init --template gascity --default-provider codex --skip-provider-readiness --no-start --name "$city_name" "$city_dir"
fi
registered_rigs() {
  # The current CLI emits setup notices before its JSON result. Keep the final
  # JSONL record as the command contract, rather than parsing human notices.
  gc rig list --city "$city_dir" --json | tail -n 1
}

rig_is_registered() {
  rig="$1"
  printf '%s\n' "$rigs_json" | jq -e --arg rig "$rig" '.rigs[] | select(.path == $rig)' >/dev/null
}

register_rig() {
  rig="$1"
  if rig_is_registered "$rig"; then
    return
  fi
  rig_name="$(basename "$rig")"
  if [ -f "$rig/.beads/metadata.json" ] && [ -f "$rig/.beads/config.yaml" ]; then
    gc rig add --city "$city_dir" --name "$rig_name" --adopt "$rig"
  else
    gc rig add --city "$city_dir" --name "$rig_name" "$rig"
  fi
}

rigs_json="$(registered_rigs)"
register_rig "$project_dir"
if [ "$review_rig_dir" != "$project_dir" ]; then
  register_rig "$review_rig_dir"
fi

rigs_json="$(registered_rigs)"
for rig in "$project_dir" "$review_rig_dir"; do
  if ! rig_is_registered "$rig"; then
    printf '%s\n' "ERROR: rig registration missing from city config: $rig" >&2
    exit 1
  fi
done
printf '%s\n' "PASS: City $city_name and configured rigs are bootstrapped"
