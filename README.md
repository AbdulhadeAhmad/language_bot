# German Learning Telegram Bot

The bot supports approved-user registration, German voice/text capture, private collections, editing, privacy controls, public discovery and copying, archived audio, and four FSRS practice modes. Use `/help` in Telegram for commands. Daily reminders are opt-in through `/reminders HH:MM TIMEZONE`; `/resume` restores an interrupted practice question. Transcription, translation, and speech generation run through private model services with durable jobs. Progress and decisions are tracked in [implementation_plan.md](implementation_plan.md).

## 🎬 See German Pal in action

<div align="center">
  <p><strong>From a German voice note to a growing, playable sentence library.</strong></p>
  <p><a href="./language_demo.mp4"><strong>▶️ Watch the demo</strong></a></p>
  <sub>Short on time? The demo opens right in GitHub's video viewer.</sub>
</div>

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
