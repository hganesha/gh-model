#!/bin/sh
# Remove batch logs older than the given number of days.
set -eu
days=14
[ "${1:-}" = "--days" ] && days="$2"
find "${BATCH_LOGS:-/var/log/batch}" -name '*.log' -mtime +"$days" -delete
