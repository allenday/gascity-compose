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

if grep -Fq '|| true' "$script"; then
  printf '%s\n' 'city bootstrap must not hide rig registration failures' >&2
  exit 1
fi

printf '%s\n' 'PASS: city bootstrap fails closed on rig registration'
