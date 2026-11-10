# Running the application

The API handles Telegram commands and callbacks. Users can save German text or voice, edit and delete private entries, manage collection privacy, discover and copy public sentences, and practice four review modes. Transcription, translation, and opt-in German speech generation use private model services. Durable workers persist job state and recover expired leases after a restart.

Use `/help` for the complete command list. `/library` browses saved entries; `/entry ID` opens one. Collections start private. Identical submissions remain separate; matches against your own or public entries produce a small advisory warning. Other users' private entries never trigger warnings. Migrations preserve prior entries and recording references. Back up the database before upgrading, then use the Compose commands below to apply migrations.

## Practice and reminders

`/practice de_to_en`, `/practice en_to_de`, `/practice dictation`, and `/practice listening` start FSRS sessions. An active question and submitted answer are persisted; `/resume` restores the current question after an interruption. Grades and session advancement are committed together, so a repeated or stale callback cannot apply a second grade.

Daily reminders are opt-in. Set a local time and IANA timezone with `/reminders HH:MM TIMEZONE`, for example `/reminders 20:00 Europe/Berlin`; use `/reminders off` to disable them. The scheduler only sends when review cards are due, initializes missing cards for older saved entries, and sends at most one reminder per local date. It skips missed reminder windows after downtime rather than sending a backlog. A spring-forward time that does not exist advances to the first valid local minute; a fall-back repeated minute is fenced by the local-date marker. Reminder delivery failures are isolated per recipient.

## Original-recording archive (opt-in)

`TELEGRAM_ARCHIVE_ENABLED` defaults to `false`. Leave it disabled until the owner approves copying original recordings into the private channel identified by `TELEGRAM_MEDIA_CHANNEL_ID`. The bot needs permission to post there. No recordings are copied while disabled; captures and translation continue normally.

After approval, set `TELEGRAM_ARCHIVE_ENABLED=true` in the external `.env` and recreate services. Migration `0005_archive_jobs` queues existing original recordings; new captures enqueue their archive job in the same transaction as their saved entry. The API's separate archive runner copies the original voice/audio message into the channel, strips its caption and keyboard, and stores the archive message reference. It uses Telegram's server-side copy, so there is no local original-audio upload file to retain. The job's destination is fixed on its first attempt. Disabling archiving stops new claims after the API is recreated; an already-issued Telegram request may still complete.

Archive jobs use row locks, leases, heartbeats, fenced completion, and up to three copy attempts per retry cycle. Rate limits honor Telegram's Retry-After delay. Network failures can have uncertain outcomes: another attempt may leave an extra archive message, but it cannot create a second active database asset or overwrite a newer result. Structured `archive_result_uncertain` and `archive_orphan_possible` events identify the media, and include the destination/message ID when a receipt is known, for manual archive cleanup. Do not delete archive messages referenced by active assets.

Entry details show original-recording storage state. Owners can retry failed storage from the entry's button when archiving is enabled. A missing/deleted source message or insufficient channel rights fails without discarding the saved text, translation, or original Telegram file reference. Ready originals have a Play original recording button. Playback copies the archived message without forwarding attribution or captions, and rechecks owner/approval on every button press. Model-service temporary files keep their existing success/failure cleanup and periodic/startup orphan cleanup.

## Generated German audio (opt-in)

`TELEGRAM_TTS_ENABLED` defaults to `false` and requires enabled archive storage. Keep generation disabled until the owner explicitly approves uploading generated speech from saved German text into the private archive channel. Enabling this feature exposes Generate German audio on saved entries and completed capture messages; it does not backfill or automatically generate audio for existing entries. The configured provisional voice is Piper Thorsten medium; final model/license approval remains part of M1.

Migration `0006_audio_jobs` adds a durable on-demand synthesis/upload queue. Multiple presses share one active generated asset per sentence/voice. Successful synthesis survives upload retries; a manual retry of failed synthesis uses a new model request ID. Long voice transcripts are synthesized in chunks of at most 1,000 characters into one WAV, up to the existing 10,000-character transcription limit. The private model API retains its WAV endpoint and supports `GET /jobs/ID/audio?format=mp3` for Telegram-compatible audio. MP3 encoding runs outside the event loop and uses memory rather than permanent extra files.

Successful upload stores the Telegram file/archive references and releases the temporary model WAV through the model API. The completed capture's buttons are updated to offer Play German audio; if that message is unavailable, reopening the entry provides the same control. Playback reuses the archive receipt and never reruns TTS. Failed upload preserves text/translation and successful synthesis; the entry shows the failed step and an owner-only retry button. Expired intermediate model output triggers synthesis under a new stable request identity. Leases, heartbeats, revision/claim fencing, bounded retries, and uncertain-upload logging follow the original archive policy.

Disabling TTS stops new generation requests/claims after API recreation; archived audio remains playable by its owner. Generated storage messages use the German sentence as their title and MP3 filename, with the voice name as performer. HTML archive captions show bold German/English sections and metadata with searchable hashtags: `#piper`, `#de_DE_thorsten_medium`, `#sentence_UUIDHEX`, `#user_ID`, `#audio_UUIDHEX`, and `#revision_N`. UUID hashtags omit hyphens so Telegram recognizes the entire identifier. Both texts are shortened where necessary to fit the caption limit while retaining metadata. HTML in sentence text is escaped. User IDs appear only in the archive caption; playback strips that caption. Generated audio activation and any live upload test must use the explicitly approved channel.

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
docker compose up -d --remove-orphans --wait --wait-timeout 180
curl --fail http://127.0.0.1:8060/health/ready
docker compose ps
```

No shell wrapper or sourced environment is required for Docker commands. Compose reads `~/language_bot_data/.env` directly for each service. To use another data location, export `LANGUAGE_BOT_DATA_DIR` before running Compose; the default works without it. The stable Compose project is `language-bot`. PostgreSQL persists in `$LANGUAGE_BOT_DATA_DIR/postgres`, with no host port. App logs, temporary files, and caches use external bind mounts; app containers do not mount the repository. Image-installed application dependencies are disposable. Models live under `models/runtime/` and mount read-only into the inference services. The downloader writes there and uses the host UID/GID (defaults 1000:1000; override LOCAL_UID/LOCAL_GID in your shell when needed). ASR uses Whisper small INT8, translation uses OPUS-MT de→en, and TTS uses Piper Thorsten medium. Model download revisions match the inspected benchmarks. Old ASR_MODEL/TRANSLATION_MODEL/GERMAN_TTS_VOICES environment entries are obsolete and ignored; changing a provider now belongs to its service rather than bot settings. Downloads run only in the explicit setup command, never during inference. To prepare just one service, use `docker compose run --rm prepare-models python -m app.inference.download --only tts` (or asr/translation).

Image builds explicitly reinstall the local `language-bot` distribution and compare every installed Python module with its source file. This prevents uv's build cache from serving a stale application wheel to console entry points while `/app/app` contains newer source. When inspecting deployed modules, `python -P` avoids accidentally importing working-directory source instead of the installed console package. `archive_audio_metadata_confirmed` logs compare Telegram's actual upload response against the expected caption, title, filename, and hashtag presence without logging sentence text.

The host port binds **only** `127.0.0.1:8060`. The process listens on `0.0.0.0:8060` inside Docker so its published port works. Configure your host nginx HTTPS server with:

```nginx
location = /telegram/webhook {
    proxy_pass http://127.0.0.1:8060;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 512k;
}
```

The current production HTTPS endpoint is `https://tglgwh.abdulhade.com/telegram/webhook`, forwarded by the host nginx to `127.0.0.1:8060`. Telegram's `getWebhookInfo` check on 2026-11-10 reported this URL, no pending updates or recent webhook errors, and allowed updates `message` and `callback_query`.

Register or change the webhook only as an explicit operational action after the HTTPS route works:

```bash
docker compose exec api python -m app.bot.webhook --url https://tglgwh.abdulhade.com/telegram/webhook
```

Startup never registers or changes a real webhook automatically. Webhook parsing retains the secret check and body-size limit. It processes short native handlers immediately and discards the Telegram update object. `incoming_updates` and `telegram_deliveries` no longer exist.

## Operations, model APIs, and recovery

A capture creates one `operations` row: owner/chat/source message IDs, optional Telegram file ID, status message ID, current step, German/English results, and lease/retry fields. A unique chat/message pair prevents webhook redelivery from creating another capture; separate intentional submissions remain separate. `/status` lists your recent operations; `/retry UUID` resumes your failed operation without discarding completed steps. No new status message is sent when its existing ID is known. Text submissions are limited to 1,000 characters; two-minute audio may produce a longer transcript. If the combined result exceeds Telegram’s message size, the status shows explicitly shortened previews and `/result UUID` returns the full saved text in chunks to its owner.

An API-owned background runner polls this table (four concurrent operations by default). It downloads voice bytes only when needed, awaits the ASR API, persists the German result, advances to translation, persists English, then edits the original status message. No database transaction spans Telegram or inference calls. Leases heartbeat every 10 seconds and expire after 60 seconds; result writes require the matching claim token. Revoked access is checked before each step. Shutdown/restart resumes pending operations from PostgreSQL.

`asr`, `translation`, and `tts` are separate containers on the internal `models` network. They have **no published ports or outbound Internet access**. Only the bot API also joins the outward-facing network for Telegram. Each model loads once and processes one durable job at a time in a thread, leaving its HTTP event loop responsive. Each service accepts at most 32 pending/running jobs by default and returns 429 when full. Set MODEL_THREADS / MODEL_QUEUE_LIMIT in Compose to tune CPU and capacity. The API stays usable if a model is down; affected operations retry, then show a retry instruction.

Internal HTTP contract (same paths for each service):

- `PUT /jobs/{UUID}`: ASR accepts raw audio bytes (20 MiB maximum); translation accepts JSON `{"text":"..."}` up to 10,000 characters (to cover two-minute transcripts); TTS accepts up to 1,000 characters. A stable ID and identical input return the existing job; different input with that ID returns 409.
- `GET /jobs/{UUID}`: pending/running/done/failed status and text output or audio availability. Clients await completion by asynchronously polling; HTTP disconnects do not delete work.
- `GET /jobs/{UUID}/audio`: completed TTS WAV. The API requests TTS through this private job interface when an owner asks to generate a voice.
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
umask 077
mkdir -p "$LANGUAGE_BOT_DATA_DIR/backups"
docker compose exec -T db pg_dump -U language_bot -d language_bot -Fc > "$LANGUAGE_BOT_DATA_DIR/backups/database.dump"
sha256sum "$LANGUAGE_BOT_DATA_DIR/backups/database.dump"
```

The `umask` keeps the dump private. Copy it off-host for protection from disk loss. The database includes users, access state, entries, jobs, settings, and FSRS review state; it stores Telegram media references, not audio blobs. Back up `.env` and model files separately. Restored Telegram media references remain usable only with the same bot and archive messages.

For a clean destination, initialize the external data directory and `.env`, then restore the same pinned models under `$LANGUAGE_BOT_DATA_DIR/models/runtime` or download them with the explicit `prepare-models` profile. Restore into the fresh database before starting application services:

```bash
source scripts/env.sh app
bash scripts/init_data.sh
docker compose up -d db
# Wait until db is healthy before restoring.
docker compose exec -T db pg_restore -U language_bot -d language_bot --no-owner < "$LANGUAGE_BOT_DATA_DIR/backups/database.dump"
docker compose up -d --build --remove-orphans --wait --wait-timeout 180
docker compose exec api alembic check
docker compose ps
```

For a restore exercise, use a separate data root and bot/database credentials in a staging environment. Confirm the Alembic revision, owner and review counts, and perform a Telegram playback check against the same bot/archive before treating the restore as accepted. To redeploy from another checkout, use the same absolute data root and standard Compose commands; users, configuration, database, models, and reports survive deletion of the source checkout. `docker compose down` does not remove bind-mounted data. Keep uncommitted source changes in version control before deleting a checkout.

For the first-release Telegram acceptance run, use two test accounts approved by the configured administrator with `/admin grant TELEGRAM_ID`. Follow [acceptance_scenarios.md](acceptance_scenarios.md) and record the two-user privacy/isolation results before marking M10 Done.
