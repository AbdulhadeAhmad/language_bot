# Running the application

The API handles commands and replies directly through AsyncTeleBot. Only domain operations are durable: German voice/text capture advances through transcription and translation using private model services. The same status message ends with both texts. TTS is available internally; bot audio generation/playback is deferred. Library management and reviews remain future work.

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
docker compose build
# First run: download the selected, pinned models directly on this server.
docker compose run --rm prepare-models
docker compose up -d --remove-orphans
curl --fail http://127.0.0.1:8060/health/ready
docker compose ps
```

No shell wrapper or sourced environment is required for Docker commands. Compose reads `~/language_bot_data/.env` directly for each service. To use another data location, export `LANGUAGE_BOT_DATA_DIR` before running Compose; the default works without it. The stable Compose project is `language-bot`. PostgreSQL persists in `$LANGUAGE_BOT_DATA_DIR/postgres`, with no host port. App logs, temporary files, and caches use external bind mounts; app containers do not mount the repository. Image-installed application dependencies are disposable. Models live under `models/runtime/` and mount read-only into the inference services. The downloader writes there and uses the host UID/GID (defaults 1000:1000; override LOCAL_UID/LOCAL_GID in your shell when needed). ASR uses Whisper small INT8, translation uses OPUS-MT de→en, and TTS uses Piper Thorsten medium. Model download revisions match the inspected benchmarks. Old ASR_MODEL/TRANSLATION_MODEL/GERMAN_TTS_VOICES environment entries are obsolete and ignored; changing a provider now belongs to its service rather than bot settings. Downloads run only in the explicit setup command, never during inference. To prepare just one service, use `docker compose run --rm prepare-models python -m app.inference.download --only tts` (or asr/translation).

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

Startup never registers or changes a real webhook automatically. Webhook parsing retains the secret check and body-size limit. It processes short native handlers immediately and discards the Telegram update object. `incoming_updates` and `telegram_deliveries` no longer exist.

## Operations, model APIs, and recovery

A capture creates one `operations` row: owner/chat/source message IDs, optional Telegram file ID, status message ID, current step, German/English results, and lease/retry fields. A unique chat/message pair prevents webhook redelivery from creating another capture; separate intentional submissions remain separate. `/status` lists your recent operations; `/retry UUID` resumes your failed operation without discarding completed steps. No new status message is sent when its existing ID is known. Text submissions are limited to 1,000 characters; two-minute audio may produce a longer transcript. If the combined result exceeds Telegram’s message size, the status shows explicitly shortened previews and `/result UUID` returns the full saved text in chunks to its owner.

An API-owned background runner polls this table (four concurrent operations by default). It downloads voice bytes only when needed, awaits the ASR API, persists the German result, advances to translation, persists English, then edits the original status message. No database transaction spans Telegram or inference calls. Leases heartbeat every 10 seconds and expire after 60 seconds; result writes require the matching claim token. Revoked access is checked before each step. Shutdown/restart resumes pending operations from PostgreSQL.

`asr`, `translation`, and `tts` are separate containers on the internal `models` network. They have **no published ports or outbound Internet access**. Only the bot API also joins the outward-facing network for Telegram. Each model loads once and processes one durable job at a time in a thread, leaving its HTTP event loop responsive. Each service accepts at most 32 pending/running jobs by default and returns 429 when full. Set MODEL_THREADS / MODEL_QUEUE_LIMIT in Compose to tune CPU and capacity. The API stays usable if a model is down; affected operations retry, then show a retry instruction.

Internal HTTP contract (same paths for each service):

- `PUT /jobs/{UUID}`: ASR accepts raw audio bytes (20 MiB maximum); translation accepts JSON `{"text":"..."}` up to 10,000 characters (to cover two-minute transcripts); TTS accepts up to 1,000 characters. A stable ID and identical input return the existing job; different input with that ID returns 409.
- `GET /jobs/{UUID}`: pending/running/done/failed status and text output or audio availability. Clients await completion by asynchronously polling; HTTP disconnects do not delete work.
- `GET /jobs/{UUID}/audio`: completed TTS WAV. TTS has no automatic bot call yet.
- `DELETE /jobs/{UUID}`: release completed/failed job input and output artifacts while retaining a result receipt. Active jobs return 409.
- `GET /health/ready`: database schema and model loaded.

Model queues are rows in `model_jobs`, filtered by service kind. They contain inference input/result fields, never raw Telegram messages. Artifacts live in external `jobs/{kind}/`; terminal artifacts and orphan files expire after 24 hours, so download TTS audio before then. Text result receipts remain for retry idempotency. Input validation failures fail immediately; transient model failures retry up to three attempts. Operation network failures also retry with a delay; a manual retry creates a new model request generation.

The first status send cannot be atomic with a PostgreSQL commit. If it times out or the API dies before storing its message ID, the operation becomes failed/uncertain rather than silently sending another message. Use `/status` and explicitly `/retry UUID`; this may create a replacement status if the original ID was lost. Ordinary command replies are direct, not durably retried; a repeated `/start` shows current access and `/admin` always lists pending users even if a channel notice failed. Ambiguous channel sends are not automatically repeated.


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

If an earlier Compose deployment has data in a named PostgreSQL volume, dump it with the old deployment before switching Compose files. Restore the dump into the new bind-mounted database, then apply migrations; do not copy a live PostgreSQL data directory. Migration `0003_operations` preserves users, access audit entries, and known approval-channel message IDs, then intentionally drops the old raw-update inbox and delivery outbox. Existing pending generic deliveries are not replayed. Take a database backup before upgrading; restoring that backup is the downgrade path.


### Upgrading from the two-worker version

Run these commands from the updated checkout before starting the new API. The backup happens before the migration removes old message tables:

```bash
docker compose stop api
# Remove old workers even though the new Compose file no longer defines them.
docker rm -f language-bot-interaction-worker-1 language-bot-delivery-worker-1
mkdir -p "${LANGUAGE_BOT_DATA_DIR:-$HOME/language_bot_data}/backups"
docker compose exec -T db pg_dump -U language_bot -d language_bot -Fc > "${LANGUAGE_BOT_DATA_DIR:-$HOME/language_bot_data}/backups/before-operations.dump"
bash scripts/init_data.sh
docker compose build
docker compose run --rm prepare-models
docker compose up -d --remove-orphans
```

The init script now repairs existing runtime-file ACLs recursively and adds `jobs/`. API logs include container hostname plus PID to avoid the earlier cross-container filename collision. PostgreSQL ownership is not changed by this script.

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
