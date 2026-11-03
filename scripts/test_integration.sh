#!/usr/bin/env bash
# Uses only a newly created disposable database. Never touches the Compose database.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh app
test_container=$(docker run --detach --rm \
  --name "language-bot-test-$$" \
  -e POSTGRES_PASSWORD=test-only-local \
  -e POSTGRES_DB=language_bot_test \
  -p 127.0.0.1::5432 postgres:16-bookworm)
trap 'docker rm --force "$test_container" >/dev/null' EXIT
ready=false
for attempt in $(seq 1 60); do
  if docker exec "$test_container" pg_isready -U postgres -d language_bot_test >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" != true ]; then
  docker logs "$test_container"
  exit 1
fi
test_endpoint=$(docker port "$test_container" 5432/tcp)
test_port=${test_endpoint##*:}
export TEST_DATABASE_URL="postgresql+asyncpg://postgres:test-only-local@127.0.0.1:$test_port/language_bot_test"
uv run --no-sync pytest tests/integration "$@"
