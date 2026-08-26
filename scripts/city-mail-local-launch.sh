#!/usr/bin/env sh
set -eu

: "${CITY_MAIL_LAUNCHER_QUEUE:?CITY_MAIL_LAUNCHER_QUEUE must be set}"
exec env CITY_MAIL_LOCAL_LAUNCHER_WORKER=true python3 /usr/local/bin/city_mail_launcher.py
