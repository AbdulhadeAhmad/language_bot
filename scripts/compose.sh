#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh app
exec docker compose --env-file "$LANGUAGE_BOT_DATA_DIR/.env" "$@"
