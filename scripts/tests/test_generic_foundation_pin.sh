#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
expected=2bfbf2a1a502491031702d483286782bd16382af

test -f "$root/.gitmodules"
test -f "$root/upstream/gascity-compose/compose.yaml"
test "$(git -C "$root/upstream/gascity-compose" rev-parse HEAD)" = "$expected"
grep -Fq 'cyberstorm-dev/gascity-compose' "$root/.gitmodules"
grep -Fq "$expected" "$root/README.md"

printf '%s\n' 'PASS: Allenday overlay pins the generic Compose foundation'
