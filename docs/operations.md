# Running the application

The application uses one admin, PostgreSQL-backed registration and approval, native AsyncTeleBot handlers, and durable outgoing actions. Sentence capture and reviews are still upcoming milestones.

## External data setup

From any checkout, with Docker Compose and uv installed:

```bash
export LANGUAGE_BOT_DATA_DIR="$HOME/language_bot_data"
bash scripts/init_data.sh
```

Install the OS `acl` package if the script requests it. The script creates external directories and a placeholder `.env` only if absent, then grants container UID 10001 access to its runtime directories. Edit `$LANGUAGE_BOT_DATA_DIR/.env` with your bot token, random webhook secret, database password/URL, `ADMIN_TELEGRAM_ID`, and `ADMIN_NOTIFICATION_CHANNEL_ID`. The database URL uses hostname `db`. The configured admin is always authorized. There is no environment user allowlist.

Add the bot to the notification channel with permission to post and edit its messages. Start a private conversation with the bot as admin. Channel members cannot approve users unless their Telegram ID equals the configured admin. `/admin` provides paginated lists and status counts; `/admin grant TELEGRAM_ID` preapproves someone who has not started the bot yet. That person receives their status after starting the bot; Telegram cannot notify a person who has never contacted it.

New private `/start` users become pending and produce one channel request. Repeat starts refresh their profile but do not create another request. Rejection/revocation remains in force until the admin restores access. Decisions and notification delivery are separate: failed delivery cannot roll back an access change.

## Start and expose the webhook

```bash
docker compose up -d --build
curl --fail http://127.0.0.1:8060/health/ready
docker compose ps
```

No shell wrapper or sourced environment is required for Docker commands. Compose reads `~/language_bot_data/.env` directly for each service. To use another data location, export `LANGUAGE_BOT_DATA_DIR` before running Compose; the default works without it. The stable Compose project is `language-bot`. PostgreSQL persists in `$LANGUAGE_BOT_DATA_DIR/postgres`, with no host port. App logs, temporary files, and caches use external bind mounts; app containers do not mount the repository. Image-installed application dependencies are disposable. Models are external and currently unused by the foundation services; future inference workers should mount the models directory read-only.

The host port binds **only** `127.0.0.1:8060`. The process listens on `0.0.0.0:8060` inside Docker so its published port works. Configure your host nginx HTTPS server with:

```nginx
location = /telegram/webhook {
    proxy_pass http://127.0.0.1:8060;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 512k;
}
```

Register the webhook once the HTTPS route works:

```bash
docker compose exec api python -m app.bot.webhook --url https://YOUR_DOMAIN/telegram/webhook
```

Startup never registers or changes a real webhook automatically. Native Telegram parsing retains the secret check, body-size limit, and durable update-ID deduplication. Handler errors propagate and roll back their transaction. Multiple outgoing effects have stable action keys. Ambiguous external send outcomes remain `uncertain` instead of being blindly resent. Review failed/uncertain rows in `telegram_deliveries` when diagnosing a missing notification; rerun only after checking the actual Telegram outcome.

## Development and benchmarks

Always source the environment script before host Python/uv commands:

```bash
source scripts/env.sh app
uv sync --locked
uv run --no-sync pytest tests/unit -q
bash scripts/test_integration.sh -q
source scripts/env.sh benchmarks
uv sync --project benchmarks --locked
uv run --project benchmarks --no-sync python benchmarks/prepare.py
uv run --project benchmarks --no-sync python benchmarks/run_all.py --only asr
```

For Qwen use `source scripts/env.sh qwen` before synchronizing its project. Switch back to `benchmarks` for the runner and pass `--qwen-python "$LANGUAGE_BOT_DATA_DIR/venvs/qwen/bin/python"`. Application and benchmark environments are separate, both Python 3.12. Environments, interpreter downloads, bytecode, test/lint caches, model caches, and temporary files go under the external root. Direct unsourced uv commands can create a repo-local environment; use the documented commands.

Models and their live manifest are in `models/`; public audio and result folders are in `benchmarks/`. Benchmark reports retain original measured server paths as historical evidence, while `review.html` uses portable audio links.

## Migrating an existing checkout

```bash
source scripts/env.sh app
python3 scripts/migrate_artifacts.py
```

This copies old `benchmarks/artifacts`, dereferences symlinks, checks every copied file's SHA-256, preserves the source manifest, and relocates live manifest paths. It refuses conflicting destination contents and never deletes originals. Review its successful output before removing old artifacts. Recreate external environments using `uv sync`; moving a virtualenv breaks embedded paths.

If a real repo-local `.env` already exists, copy it into the external root without overwriting an existing configuration, replace `ALLOWED_TELEGRAM_USER_IDS` with the two admin settings, and keep the original until startup succeeds. The old allowlist is not automatically imported: grant users through `/admin`.

If an earlier Compose deployment has data in a named PostgreSQL volume, dump it with the old deployment before switching Compose files. Restore the dump into the new bind-mounted database, then apply migrations; do not copy a live PostgreSQL data directory. The new migration retains inbox/outbox data. Downgrading refuses a multi-action outbox until it has been drained and archived.

## Backup, restore, and checkout deletion

```bash
source scripts/env.sh app
docker compose exec -T db pg_dump -U language_bot -d language_bot -Fc > "$LANGUAGE_BOT_DATA_DIR/backups/database.dump"
```

For a fresh destination, initialize external configuration, start only PostgreSQL, restore the dump, then start the app:

```bash
docker compose up -d db
# Wait until db is healthy before restoring.
docker compose exec -T db pg_restore -U language_bot -d language_bot --no-owner < "$LANGUAGE_BOT_DATA_DIR/backups/database.dump"
docker compose up -d --build
```

Back up `.env` and any desired model/benchmark files separately. Keep backups outside this host as well if recovery from disk loss is required. To redeploy from another checkout, use the same absolute data root and standard `docker compose` commands; users, configuration, database, models, and reports survive deletion of the source checkout. `docker compose down` does not remove bind-mounted data. Keep any uncommitted source changes in version control before deleting a checkout.
