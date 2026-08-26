#!/usr/bin/env sh
# Reconcile the private City-mail deployment, then prove a fresh real-run handoff.
# This never reuses a prior fixture issue or mutates forensic evidence.
set -eu

env_file="${ENV_FILE:-.env}"

make gitea-mail-launcher-up ENV_FILE="$env_file"
CITY_MAIL_LAUNCHER_SMOKE_SKIP_UP=true make gitea-mail-launcher-smoke ENV_FILE="$env_file"
