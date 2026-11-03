#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh app
mkdir -p "$LANGUAGE_BOT_DATA_DIR/cache/app"
if [ ! -e "$LANGUAGE_BOT_DATA_DIR/.env" ]; then
  (umask 077; cp .env.example "$LANGUAGE_BOT_DATA_DIR/.env")
fi
# Shared runtime directories only; never alter PostgreSQL ownership here.
# Container UID 10001 needs these paths writable. Use ACLs rather than world-write.
if command -v setfacl >/dev/null; then
  setfacl -m u:10001:rwx "$LANGUAGE_BOT_DATA_DIR/tmp" "$LANGUAGE_BOT_DATA_DIR/cache/app" "$LANGUAGE_BOT_DATA_DIR/logs"
  setfacl -d -m u:10001:rwx "$LANGUAGE_BOT_DATA_DIR/tmp" "$LANGUAGE_BOT_DATA_DIR/cache/app" "$LANGUAGE_BOT_DATA_DIR/logs"
else
  echo 'Install the acl package, then rerun this script to grant container UID 10001 access.' >&2
  exit 1
fi
printf 'Configuration: %s/.env\n' "$LANGUAGE_BOT_DATA_DIR"
