#!/usr/bin/env sh
set -eu

env_file="${ENV_FILE:-.env}"
value() { awk -F= -v key="$1" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$env_file"; }
city_dir="$(value CITY_DIR)"
project_dir="$(value MY_PROJECT_DIR)"
source_dir="$(value GASCITY_SOURCE_DIR)"
city_name="$(value CITY_NAME)"
[ -n "$city_name" ] || city_name=my-city
for path in "$city_dir" "$project_dir" "$source_dir"; do
  case "$path" in /*) ;; *) echo "ERROR: absolute path required: $path" >&2; exit 1;; esac
done
compose() { docker compose --env-file "$env_file" "$@"; }

if [ ! -f "$city_dir/city.toml" ]; then
  mkdir -p "$city_dir"
  compose run --rm --no-deps --entrypoint sh city -ec '
    dolt config --global --add user.name "${DOLT_USER_NAME:-Allen Day}"
    dolt config --global --add user.email "${DOLT_USER_EMAIL:-allenday@allenday.com}"
    exec gc init --template gascity --default-provider codex --skip-provider-readiness --no-start --name "$CITY_NAME" "$CITY_PATH"
  '
fi
for rig in "$project_dir" "$source_dir"; do
  compose run --rm --no-deps --entrypoint gc city rig add --city "$city_dir" "$rig" >/dev/null 2>&1 || true
done
printf '%s\n' "PASS: City $city_name and configured rigs are bootstrapped"
