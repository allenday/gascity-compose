#!/usr/bin/env sh
set -eu

: "${CITY_PATH:?CITY_PATH must be set}"
: "${CITY_MAIL_SIGNAL_FILE:?CITY_MAIL_SIGNAL_FILE must be set}"

gc_command="${CITY_MAIL_WAKE_GC:-gc}"
poll_seconds="${CITY_MAIL_WAKE_POLL_SECONDS:-1}"
timeout_seconds="${CITY_MAIL_WAKE_TIMEOUT_SECONDS:-15}"
last_signal=

while :; do
  if [ -r "$CITY_MAIL_SIGNAL_FILE" ]; then
    current_signal="$(cksum <"$CITY_MAIL_SIGNAL_FILE" 2>/dev/null || true)"
    if [ -n "$current_signal" ] && [ "$current_signal" != "$last_signal" ]; then
      if timeout --signal=TERM "$timeout_seconds" "$gc_command" --city "$CITY_PATH" \
        session nudge --delivery=wait-idle mayor \
        'You have authenticated tracker mail in Agent Mail. Fetch it through the Mayor-only Agent Mail MCP binding; follow Superpowers and IDD planning; respond on the linked Gitea issue before internal ceremony. When publishing an authenticated intake plan, the standalone gascity:intake-plan:v1 machine plan marker must contain only revision and repository JSON fields; use a new revision after every amendment.' >/dev/null 2>&1; then
        last_signal="$current_signal"
        if [ "${CITY_MAIL_WAKE_ONCE:-false}" = "true" ]; then
          exit 0
        fi
      fi
    fi
  fi
  sleep "$poll_seconds"
done
