#!/usr/bin/env bash
# Compatibility alias; use docker compose directly for new commands.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose "$@"
