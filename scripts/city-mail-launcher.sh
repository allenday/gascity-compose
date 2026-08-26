#!/usr/bin/env sh
set -eu

: "${CITY_MAIL_LAUNCHER_SECRET_FILE:?CITY_MAIL_LAUNCHER_SECRET_FILE must be set}"
: "${CITY_MAIL_LAUNCHER_STATE:?CITY_MAIL_LAUNCHER_STATE must be set}"
test -r "$CITY_MAIL_LAUNCHER_SECRET_FILE"
exec python3 /usr/local/bin/city_mail_launcher.py
