# German Learning Telegram Bot

The bot supports registration, admin approval, and durable German voice/text processing. Commands run directly in the API. Private ASR, translation, and TTS services own PostgreSQL-backed model queues; the bot currently returns German and English text, with audio playback deferred. Library management and reviews are upcoming milestones. Progress and decisions are tracked in [implementation_plan.md](implementation_plan.md).

- [Setup, deployment, admin workflow, external storage, and migration](docs/operations.md)
- [Acceptance scenarios](docs/acceptance_scenarios.md)
- [Model benchmarks](benchmarks/README.md)

```bash
bash scripts/init_data.sh
# Edit ~/language_bot_data/.env with your credentials and admin/channel IDs.
docker compose build
docker compose run --rm prepare-models
docker compose up -d --remove-orphans
```

nginx can reach the bot at `127.0.0.1:8060`. All persistent configuration, PostgreSQL data, models, results, and runtime files live outside the source checkout under `~/language_bot_data` (override with absolute `LANGUAGE_BOT_DATA_DIR`).

Development verification:

```bash
source scripts/env.sh app
uv sync --locked
uv run --no-sync pytest tests/unit -q
bash scripts/test_integration.sh -q
python3 -m unittest discover -s benchmarks/tests -q
```
