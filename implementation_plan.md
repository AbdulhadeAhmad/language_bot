# German Learning Bot: Implementation and Verification Plan

This document is the implementation source of truth agreed during planning. Where it differs from `project_description.md`, the decisions recorded here take precedence.

## Progress tracking

Move each milestone through **Not started → In progress → Verification → Done**. Check its checkbox only after its acceptance checks pass, and record test evidence and relevant changes in this table. Record any later agreed changes in the decision log at the end of this document.

| Milestone | Status | Evidence / notes |
|---|---|---|
| M0 — Specification and tracking | Done | [30 acceptance scenarios](docs/acceptance_scenarios.md) cover ownership, duplicates, privacy, copying, editing, deletion, and review behavior; checked against the revised specification. |
| M1 — Runtime and model feasibility | In progress | [First server-run findings](docs/benchmark_findings_20261103.md): translation and three TTS voices completed. ASR rerun passed; [second-run findings](docs/benchmark_findings_20261104.md) compare accuracy and expanded TTS performance. [Round-two commands](benchmarks/README.md) compare six Piper variants and three Qwen3-TTS 0.6B speakers over 27 inputs. Quality, license review, and final selection remain pending. |
| M2 — Async application foundation | Done | Owner accepted completion on 2026-11-05 after successfully testing live transcription and translation. Current deployment and model smoke checks passed; fresh-deployment/recreation checks for the latest refactor were not rerun. [Operations](docs/operations.md). |
| M3 — Collections and duplicate warnings | Done | Personal sentence/media/voice schema, completed-capture migration, `/library` pagination, owner-only entry controls, normalized non-unique lookup, and private-safe advisory warnings implemented. 14 unit and 33 PostgreSQL integration tests pass, including concurrency, redelivery/retry idempotency, ownership, privacy, and migration preservation/rollback. Current deployment verified healthy with schema and collection smoke checks. |
| M4 — Durable jobs and archive storage | Done | Archive queue, migration/backfill, server-side media adapter, leases/heartbeats, bounded retries, orphan logging, and owner-only retry controls implemented. 17 unit and 40 PostgreSQL integration tests pass. Owner explicitly approved and activated copying to the configured private channel; 4 live recordings archived successfully in one attempt each, with no duplicate assets/receipts. All services healthy and deployed schema matches code. |
| M5 — Voice and text capture | Done | Capture/library/archive flow, original playback, and on-demand Thorsten speech generation deployed healthy. 22 unit and 48 PostgreSQL integration tests pass. Owner explicitly approved generated-audio activation; live synthesis, MP3 archive upload/read-back, owner playback controls, and model-file cleanup verified. Multi-user/discovery behavior will be extended at M7. |
| M6 — Editing and deletion | In progress | Persisted, owner-scoped paired German/English edits with `/cancel`, advisory duplicate recalculation, revision increment, and stale generated-audio invalidation implemented. Focused PostgreSQL edit coverage passes; remaining audio replacement/deletion and whole-entry deletion are still open. |
| M7 — Privacy, discovery, and copying | Not started | |
| M8 — Practice and FSRS | Not started | |
| M9 — Reminders and recovery | Not started | |
| M10 — Validation and deployment | Not started | |

## 1. Agreed product behavior

The first release supports administrator-approved users, with independent sentence collections, public discovery, audio management, and spaced repetition.

| Area | Agreed behavior |
|---|---|
| Access | Database-backed registration and approval through /admin; ADMIN_TELEGRAM_ID is the single protected admin; requests go to ADMIN_NOTIFICATION_CHANNEL_ID. |
| Privacy | New users start private. Private mode hides all their entries from other users. |
| Public entries | Show German, English, generated audio, and original recordings, without contributor identity |
| Discovery | Search German/English, random browsing, and latest public entries |
| Discovery duplicates | Public entries may appear separately even with identical German text, including entries from the same user |
| Copying | Copy text and generated audio into an independent personal entry; exclude original recordings |
| Copy lifetime | Copies survive source deletion or privacy changes |
| Copy provenance | Copies retain an optional reference to the sentence they were copied from, for tracking and possible future source-change notifications |
| German duplicates | Allowed globally and within each user’s collection; no text uniqueness constraints |
| Duplicate detection | Compare normalized German against the user’s own entries and other users’ public entries |
| Repeated submission | Save a separate entry and show a small, non-blocking warning when a duplicate is detected; the user can ignore it |
| German editing | Affect only the editor’s entry; regenerate translation and generated audio |
| Edit collision | Allow the edit with a small, non-blocking duplicate warning; leave matching entries unchanged |
| English/audio editing | Affect only the user’s own entry |
| Input limits | Up to 1,000 text characters or a two-minute voice recording; one entry per submission |
| Practice | German → English, English → German, dictation, and listening meaning |
| Translation practice interaction | Show the prompt and its translation hidden in a Telegram HTML spoiler, with inline grading buttons; no typed answer is required in either translation direction |
| Grading | Reveal the answer, then let the user choose Again, Hard, Good, or Easy |
| Scheduling | FSRS, with an independent schedule for each entry and practice mode |
| Reminders | Optional daily reminder at the user’s chosen time and timezone |

Private mode prevents future discovery and access through the bot. Previously saved copies remain independent. Previously delivered Telegram messages cannot be made unseen.

## 2. Architecture and data ownership

### Application structure

Use **FastAPI, SQLAlchemy async, Pydantic, and pyTelegramBotAPI’s `AsyncTeleBot`**, packaged with Docker Compose.

Run these services:

- **API:** authenticated Telegram webhook, direct AsyncTeleBot command/reply handling, health endpoints, and an async runner for durable capture operations.
- **ASR / translation / TTS:** separate private HTTP services, each owning a PostgreSQL-backed inference queue and one CPU consumer. Model inference runs outside the HTTP event loop.
- **PostgreSQL:** users, domain state, processing operations, and model jobs. Never store raw incoming Telegram update payloads or generic command-delivery work.

A voice capture persists only IDs/references and required step data. The bot immediately sends one status message; the operation progresses through transcription → translation → completed German/English text, updating that same message. Archived originals are playable by their owner. Generated German audio is optional and requested separately from saved entries; generated MP3s use the explicitly approved private archive and are playable only by their owner until discovery is implemented.

Each task uses its own async SQLAlchemy session. Transactions stay short and do not span network or model calls. Model-service jobs and capture operations have leases, heartbeats, retry limits, and fenced result writes. Retries use stable model request IDs; manual operation retry starts a new request generation. Only the bot API has outward networking; model APIs are Docker-network-only.

### Core records

| Record | Responsibility |
|---|---|
| `User` | Telegram identity, privacy, timezone, reminder and review preferences |
| `Sentence` | User-owned saved entry: owner `user_id`, German text, non-unique normalized lookup value, English translation, processing state, revision, nullable `copied_from_sentence_id`, provenance, and status-message reference |
| `Voice` | Configured German voice and provider settings |
| `AudioAsset` | Personal entry’s original or generated recording, generation state, and voice |
| `StoredMedia` | Telegram archive reference shared safely by independent asset references |
| `ReviewCard` | FSRS state for one personal entry and practice mode |
| `ReviewAttempt` | Self-grade and scheduling history, with an optional submitted answer for modes that collect one; translation flashcards do not collect an answer |
| `ReviewSession` | Current question and session progress |
| `InteractionState` | Pending text edit or replacement recording |
| `Operation` / `ModelJob` | Domain capture progress and durable per-model inference jobs; no raw Telegram updates |

Each `Sentence` belongs directly to one user; there is no separate `UserSentence` model or association table. Audio assets, review cards, and entry-specific jobs reference `Sentence.id`. Copying creates a new `Sentence` owned by the recipient, with independent text, metadata, and review state; generated media can still be shared through `StoredMedia` references.

`copied_from_sentence_id` is an indexed, nullable foreign key to `Sentence.id`. Set it to the immediate source when copying, including when copying a copy; ordinary submissions leave it null. Keep the reference after personal edits. Use `ON DELETE SET NULL` so deleting a source preserves its copies. The reference records provenance only: it never synchronizes content, grants access to a private source, or exposes its owner. Future source-change notifications may use this relationship, subject to current visibility checks; implementing notifications is outside the initial release.

Database constraints enforce:

- No global or per-user uniqueness constraint on German text or its normalized lookup value. Each intentional submission or copy creates a separate user-owned `Sentence` record.
- One active generated asset per personal entry and configured voice.
- One review card per personal entry and mode.
- Unique capture identities by chat/source message ID and stable model-job IDs; no content uniqueness constraints.

For duplicate detection, normalize German using Unicode NFC, trim leading/trailing whitespace, and collapse internal whitespace. Preserve capitalization, punctuation, umlauts, and spelling. Index the lookup value without a uniqueness constraint.

Duplicate warnings appear in the existing status/detail message without confirmation or a processing pause. Check submissions, copies, German edits, and replacement-audio transcriptions; exclude the current entry from its own comparison. Never reveal or signal matches in another user’s private collection. Concurrent submissions may both proceed; detection is best-effort, not a locking or rejection mechanism.

Intentional duplicate content is distinct from delivery retries: processing the same Telegram update or retrying the same job must still produce only one logical operation.

### Interfaces and ownership rules

Provider interfaces cover ASR, translation, TTS, media storage, and review scheduling. Pydantic models validate configuration and service inputs/outputs. Model-specific types stay inside provider adapters.

Services handle sentence management, copying, discovery, audio, processing, privacy, and reviews. Telegram handlers only interpret updates and call these services.

Copies initially reference immutable generated media. Regeneration changes only the requesting entry’s asset reference. Delete archived media only after no active asset references it.

All public reads—including playback and callbacks from old discovery messages—recheck current visibility. Send archived media without forwarding contributor or archive-channel identity.

## 3. Trackable implementation milestones

Each milestone closes only when its acceptance checks pass. M1 benchmarking continues in parallel with application development, as authorized by the owner. Use replaceable provider interfaces and provisional lightweight candidates; final model quality and license approval remain required before release.

### [x] M0 — Lock the specification and project tracking

**Deliverables**

- Record the agreed behavior above as the updated specification.
- Create a checklist using these milestone IDs.
- Document acceptance scenarios and independent personal entries, permitted duplicate German text, and shared immutable media.

**Acceptance**

- Examples cover two users saving the same German, copying, editing, switching privacy, and deleting.
- No unresolved ownership or privacy behavior remains in those examples.

### [ ] M1 — Verify runtime and local model feasibility

**Deliverables**

- Benchmark local ASR, German-to-English translation, and TTS candidates on the four-core, 16 GB server with approximately 400 GB SSD storage.
- Include faster-whisper and OPUS-MT as candidates; evaluate CPU-suitable TTS implementations with three distinct German voices.
- Record Python compatibility, installation requirements, model licenses, peak memory, cold/warm latency, and sample quality.
- Select and pin the Python version, provider packages, models, voices, and thread limits. The starter’s Python 3.14 pin may change if model compatibility requires it.

**Acceptance**

- A representative German sample set completes ASR, translation, and all three voices.
- A two-minute voice recording and 1,000-character text complete without out-of-memory failures.
- Leave memory headroom for PostgreSQL and the application; initially budget at most 10 GB for model inference.
- The project owner listens to the voice samples and accepts transcription/translation quality.
- If no candidate meets the hardware or quality requirements, resolve that constraint before proceeding.

### [x] M2 — Establish the async application foundation

**Deliverables**

- Docker Compose services, dependency lockfile, environment configuration, and Alembic migrations.
- Async SQLAlchemy session management and provider dependency injection.
- FastAPI health/readiness endpoints and secret-validated Telegram webhook.
- Database-backed registration, channel approval, and admin controls handled directly in the API; no raw Telegram payload storage.
- Structured logs with user, entry, job, and operation IDs.

**Acceptance**

- A fresh deployment migrates and starts successfully.
- Invalid webhook secrets cannot create work; pending/rejected/revoked users can only use registration/status flows, never protected application workflows.
- Repeated delivery of one capture produces one domain operation identified by chat and source message ID.
- Health distinguishes a running process from a database-ready application.

### [x] M3 — Implement personal collections and duplicate warnings

**Deliverables**

- Core user, user-owned sentence, voice, and media models; audio and review relationships reference the sentence directly.
- Transactional entry creation and normalized, non-unique lookup for advisory duplicate warnings.
- Private-by-default user settings.
- Ownership checks applied in services and database queries.

**Acceptance**

- Separate intentional identical submissions create separate entries, including simultaneous submissions and repeated submissions by the same user.
- Equivalent whitespace produces the same duplicate lookup value without merging or rejecting entries.
- Detected duplicates produce a small warning without requiring confirmation or blocking processing.
- Another user’s private entries never trigger a warning or reveal their existence.
- Capitalization and punctuation differences remain distinct.
- Users cannot read or modify another user’s private entry by manipulating callback IDs.

### [x] M4 — Build durable jobs and archive storage

**Deliverables**

- PostgreSQL job claiming using row locking, leases, heartbeats, retry scheduling, and expired-job recovery.
- Enqueue work in the same transaction as the state change that requires it.
- Explicit processing and per-audio states.
- Telegram media-storage adapter and temporary-file lifecycle.
- One status renderer reading persisted state; queued refreshes collapse into the latest state.

**Acceptance**

- Restarting a worker resumes unfinished work without duplicating active assets.
- A busy inference worker does not block bot commands.
- Network timeouts and Telegram rate limits trigger bounded retries.
- Temporary files are removed after success and failure; startup cleanup removes abandoned files.
- An uncertain Telegram upload result may leave an orphan archive message, but cannot create duplicate active database assets. Log such cases for cleanup.

### [x] M5 — Deliver voice and text capture

**Deliverables**

- Current voice flow: transcribe → translate → save personal entry and show both texts in the original status message with accessible duplicate warnings. Original recording archive jobs copy to the approved private storage channel independently of translation.
- Current text flow: translate → save personal entry and show both texts. On-demand German audio generation and archived playback are active following owner approval of generated-audio storage.
- One editable processing-status message with management controls.
- Step-specific failure information and retry actions.
- Maximum input length/duration enforcement.

**Acceptance**

- Both input types produce persisted German/English operation results and one updated status message. Generated audio is optional and requested separately from the saved entry or completed capture.
- Voice entries also retain their original recording.
- Playback reuses Telegram references.
- A failed step does not discard successful earlier results.
- Duplicate input creates a separate entry and shows an advisory warning when detected, without changing existing entries.
- Two users can submit work and continue using settings, discovery, and saved-entry controls while processing runs. Verify discovery controls once M7 is available.

### [ ] M6 — Complete editing, audio management, and deletion

**Deliverables**

- German/English edits, original-recording replacement, individual/all/missing voice generation, and audio deletion.
- Persisted edit states with `/cancel`.
- Revision checks that discard obsolete worker results.
- Staged replacement: retain the working entry until replacement outputs are usable.
- Confirmed whole-entry deletion and reference-aware media cleanup.

**Acceptance**

- German edits affect only the editor and regenerate derived content.
- English edits do not regenerate German audio.
- Editing German to match another entry succeeds with an advisory warning; matching entries remain unchanged.
- Failed replacement preserves the previous working content.
- Old jobs cannot overwrite a newer edit or resurrect a deleted entry.
- Deleting one entry cannot remove media still used by an independent copy.

### [ ] M7 — Add privacy, discovery, and copying

**Deliverables**

- Privacy settings and paginated German/English search.
- Random browsing and a latest-public-entries feed.
- Public detail/playback controls and copy-to-my-collection action.
- Persist the immediate source in `copied_from_sentence_id` when creating a copy, enabling future provenance tracking and source-change notifications.
- Literal, case-insensitive substring search initially; no semantic search.
- Anonymous public variants shown as separate entries.

**Acceptance**

- Private entries never appear in discovery.
- Switching to private immediately blocks subsequent source access through old callbacks.
- Public original recordings are playable without revealing contributor or archive identity.
- Copying includes English and generated voices, excludes original audio, and starts fresh personal review progress. Verify review initialization once M8 is available.
- Copy edits and source edits remain independent.
- Ordinary submissions have no source reference; copies point to their immediate source, and personal edits retain that reference.
- Deleting a source clears its copies’ source references without deleting the copies or their review progress. Private sources remain inaccessible through provenance links.
- Repeated intentional copies create independent entries and show a non-blocking warning when a match is detected.
- Source deletion/private mode does not invalidate an existing copy.

### [ ] M8 — Implement practice and FSRS

**Deliverables**

- Four practice modes with persisted sessions and answer reveal.
- German → English and English → German are mental-recall flashcards: send the prompt followed by its translation inside `<span class="tg-spoiler">...</span>` in the same message using HTML parse mode. Escape sentence text before rendering. See [Telegram HTML formatting](https://core.telegram.org/bots/api#html-style).
- Put inline **Again / Hard / Good / Easy** buttons under each translation flashcard immediately. The user recalls mentally, taps the spoiler to reveal the answer, and selects a grade; there is no text-entry state or separate reveal button. Do not gate grading on observing a spoiler reveal.
- Typed-answer translation exercises may be introduced later as separate practice modes with their own review cards; they are not part of these two flashcard modes or the initial release.
- FSRS scheduling through an adapter using the [Python FSRS implementation](https://github.com/open-spaced-repetition/py-fsrs).
- Target retention 90%; default 10 new cards daily across modes and 20 questions per session.
- Due cards first, then new cards in stable entry/mode order.
- User-configurable new-card and session limits.
- Random available German audio for listening prompts, without revealing the answer in captions.

**Acceptance**

- Each entry/mode has independent scheduling state, including entries with identical German text.
- Both translation directions can complete entirely through spoiler reveal and grading buttons, without sending a German or English answer.
- Telegram renders the translation as a spoiler and displays the grading buttons beneath it; HTML-sensitive sentence characters render correctly. Automated renderer tests and live Telegram checks verify this behavior.
- Translation flashcard attempts store a grade without requiring a submitted answer; normal text messages are not consumed as answers for these modes.
- Grading the same question twice updates scheduling only once.
- German changes reset all four schedules.
- English changes reset both translation directions and listening meaning; dictation remains unchanged.
- Audio-only changes retain scheduling state.
- Missing required text/audio makes only the affected mode temporarily unavailable.
- Historical attempts remain distinguishable from attempts against edited content.

### [ ] M9 — Add optional reminders and recovery checks

**Deliverables**

- User-selected timezone and daily reminder time.
- At most one reminder per local date when review cards are due.
- No historical reminder backlog after downtime.
- Recovery for interrupted review sessions, jobs, and status-message updates.

**Acceptance**

- Reminders respect opt-in, timezone changes, and daylight-saving transitions.
- Restarting services does not duplicate reminders or grades.
- A blocked Telegram recipient does not interrupt other users’ work.
- Database state can reconstruct the current processing and review UI.

### [ ] M10 — Validate and deploy the first release

**Deliverables**

- Production Compose configuration using the existing HTTPS endpoint.
- Webhook registration, archive-channel setup instructions, and operational runbook.
- Manual database backup and restore commands.
- Full acceptance run using two allowlisted Telegram accounts.

**Acceptance**

- Both accounts complete capture, editing, privacy, discovery, copying, and review scenarios.
- Worker termination during inference/upload/replacement does not corrupt existing entries.
- A backup restores users, entries, jobs, settings, and review state into a clean database.
- Restored Telegram media references remain usable with the same bot and archive.
- No secrets, audio blobs, or permanent generated audio files enter source control or PostgreSQL.

## 4. Verification and completion rules

Use three test layers:

- **Unit tests:** normalization, advisory duplicate detection and warning rendering, dependency invalidation, rendering, access rules, and review scheduling with a controlled clock.
- **PostgreSQL integration tests:** capture redelivery deduplication, domain and model-job leases/recovery, native admin transactions, and durable results. Extend these to collection privacy, duplicate warnings, revisions, and media references in their milestones. Use real PostgreSQL rather than SQLite.
- **Telegram acceptance tests:** actual archive/playback behavior, editable status messages, anonymous discovery, privacy transitions, and two-user isolation.

Routine automated tests use deterministic fake model and Telegram adapters. Real model benchmarks and live Telegram tests run at their designated milestones.

Track each milestone as **Not started → In progress → Verification → Done**, with recorded test evidence. Model/runtime selection is intentionally resolved by M1’s measured results; the remaining architectural and product decisions are specified above.

## 5. Decision log

| Date | Decision | Reason / reference |
|---|---|---|
| 2026-11-03 | Saved the agreed implementation plan; implementation milestones remain unstarted. | Planning conversation and project owner’s request to preserve the plan for future tracking. |
| 2026-11-03 | Removed global and per-user German uniqueness enforcement. Intentional duplicates are allowed; detected matches produce a small, ignorable warning. Updated schema rules, milestones, and acceptance tests; delivery/job idempotency remains required. | Project owner’s revised duplicate policy. |
| 2026-11-03 | Merged `Sentence` and `UserSentence` into one user-owned `Sentence` model. Copies create separate sentence records; shared media references remain independent of sentence ownership. | Global German-text uniqueness is no longer required, so the separate personal-entry model adds no value. |
| 2026-11-03 | Added nullable `copied_from_sentence_id` to track a copy’s immediate source. Preserve the link through edits and clear it on source deletion; source-change notifications remain a future feature. | Project owner requested copy provenance for tracking and later change notifications. |
| 2026-11-03 | Defined both translation directions as mental-recall flashcards with spoiler-hidden answers and inline Again/Hard/Good/Easy grading. No typed answer is required; typed-answer translation tests are separate future modes. | Project owner clarified the translation practice interaction. |
| 2026-11-03 | Prepare M1 benchmarks here for the owner to run on the target server. The development workspace reports 12 logical CPUs and approximately 23 GiB RAM, so it is not the target hardware. | Owner explicitly selected target-server benchmark preparation. |
| 2026-11-03 | Use an isolated Python 3.12 benchmark environment with a dependency lock; evaluate faster-whisper base/small INT8, OPUS-MT, and three Piper voices. These are candidates, not final provider/runtime choices. | CPU feasibility investigation; application dependencies and Python pin remain unchanged pending M1 evidence. |

| 2026-11-03 | Supply public German human-speech fixtures and a one-command benchmark runner; user recordings and manual manifests are no longer prerequisites. | Owner requested a complete ready-to-run benchmark after installing dependencies and preparing models. |

| 2026-11-03 | Expand TTS evaluation to more Piper voices plus Qwen3-TTS 0.6B on CPU, in an isolated environment. | Owner selected the broader comparison; no final TTS provider or voices approved yet. |

## 6. Execution log

### 2026-11-03 — M0 complete; M1 benchmark tooling prepared

- Added `docs/acceptance_scenarios.md` with 30 observable scenarios mapped to milestones; reviewed them against the current ownership, duplicate-warning, provenance, privacy, and spoiler-flashcard rules.
- Added isolated benchmark preparation, async subprocess orchestration, and provider experiments under `benchmarks/`. Models run sequentially with CPU affinity; reports capture load/inference timing, peak RSS, input/model identity, outputs, and errors. Downloads are separate from measured inference.
- Added six representative German text fixtures plus an exact 1,000-character stress case. Initially required user-supplied human audio; superseded by the public-fixture preparation below. Skipped ASR produces an incomplete suite, not a passing result.
- Added the server runbook and quality-review checklist. Generated audio is retained only as ignored benchmark evidence for listening review, separate from production audio-lifecycle requirements.
- Verification: `python3 -m unittest discover -s benchmarks/tests -v` passed 13 tests; CLI help and `git diff --check` passed. These are benchmark-tool tests, not real-model or application acceptance results.
- Dependency resolution completed and produced `benchmarks/uv.lock`. Local installation failed during package downloads with a network timeout; provider imports and actual inference have not been verified. The server runbook includes installation and an import check before downloading model weights.
- Remaining M1 work: run the locked environment and candidate models on the target server, inspect memory and latency, review model/voice licenses, listen to samples, and record final model/runtime decisions with owner acceptance. M2 has not started because M1 remains the agreed feasibility gate.

### 2026-11-03 — Self-contained benchmark inputs and execution

- Owner confirmed dependencies installed and `benchmarks/prepare.py` completed on the target server. No need to reinstall or redownload model weights for this update.
- Added `prepare_audio.py`: downloads Google FLEURS German test speech from the original archive, pins its dataset revision, records attribution/checksums, and reuses verified local fixtures. The preview API was tested and failed; the implementation uses the original archive instead.
- Downloaded and decoded six distinct human speech clips with dataset transcripts, plus an exact 120-second stress fixture assembled from complete utterances and silence. Verified all seven WAVs and manifest checksums. Dataset revision: `70bb2e84b976b7e960aa89f1c648e09c59f894dd`; license: CC-BY-4.0. No user recordings or manually written manifests are required.
- Added `run_all.py`: reuses prepared models, prepares or verifies public audio, runs all candidates, and writes a readable summary beside the detailed report. The initial transfer-ZIP handoff was superseded by direct server-side downloading below.
- The assembled two-minute fixture tests duration/resource handling; it does not establish accuracy on continuous conversation or learner pronunciation. Personal recordings may be used later but are not required to execute M1 benchmarks.
- Target-server model timings and owner quality approval remain pending; fixture validation does not complete M1.

- Verification for this update: all 20 benchmark-tool tests passed; live public-audio preparation and cached checksum verification succeeded; the transfer archive was integrity-checked.

### 2026-11-03 — Direct server downloads; undo committed artifact additions

- Inspected commit `1e69afd` (`added the artifacts`). Removed its generated ZIP, WAVs, attribution, and manifest from the working tree and restored the `benchmarks/artifacts/` ignore rule. Retained the downloader, runner, and tests needed for server-side preparation; Git history was not rewritten.
- The server command remains `uv run --project benchmarks --no-sync python benchmarks/run_all.py`. On a fresh checkout, it retrieves public speech and transcripts directly from Google FLEURS via Hugging Face, creates the fixtures locally, and reuses previously prepared model weights. No laptop download or artifact transfer is required.
- Updated the runbook to remove the ZIP workflow. This change is verified offline with simulated upstream data; no live laptop download is required.
- Verification: all 23 benchmark-tool tests passed, including first-run source download with simulated upstream data, transcript/archive pairing, cache reuse without network access, and cleanup after interrupted downloads. `git diff --check` passed.

### 2026-11-03 — First target-server results inspected

- Recorded findings in `docs/benchmark_findings_20261103.md`, including report identity, timings, resource usage, quality observations, and outstanding acceptance checks.
- Confirmed all 63 generated WAVs copied intact. Translation/TTS completed; neither ASR candidate reached inference because faster-whisper passed a keyword removed in the locked PyAV version.
- Identified a translation meaning loss and repeated missing-phoneme warnings for Eva/Karlsson; did not equate execution success with quality approval. M1 remains in progress.
- Next repair is to constrain PyAV below 19 and regenerate the lock, then synchronize and rerun ASR on the server. This results-inspection step did not alter dependencies or the original benchmark outputs.

### 2026-11-03 — ASR dependency repair and expanded TTS benchmark

- Added the upstream-compatible `av<19` bound and regenerated the main lock to PyAV 18.1.0. Future ASR reports include PyAV version and normalized word error rates. Actual ASR inference still needs the server rerun.
- Added group/candidate selectors so ASR can be rerun without translation/TTS, and individual voices can be retried independently. Preparation preserves existing model records and downloads only missing candidates.
- Expanded Piper to six variants (five distinct speakers), and added Qwen3-TTS 0.6B CustomVoice with Ryan, Aiden, and Serena. Qwen has a separate Python 3.12 dependency lock, CPU Torch/Torchaudio, float32 eager inference, and seeded generation. It does not alter the main Transformers environment.
- Added twenty German pronunciation/usage fixtures for a total of 27 inputs. Two repetitions over nine TTS configurations produce up to 486 WAVs. The 1,000-character stress input stays unchanged for comparison with round one.
- Added an offline HTML listening comparison, scoring CSV, warning counts, installed-package evidence, and checkpoint/log preservation on timeout. Original server reports and samples remain unchanged.
- Verification: 33 offline tests passed, covering model reuse, candidate selection, CPU Qwen adapter calls, ASR lazy inference/scoring, HTML escaping/portable audio links, and timeout evidence recovery. Both dependency locks pass offline consistency checks. These do not claim actual Qwen inference or server ASR success; those measurements and owner quality approval are pending.

### 2026-11-03 — M2 implemented; deployment verification remains

- Added the Python 3.12 application dependency lock, async FastAPI application, SQLAlchemy/asyncpg sessions, Alembic migrations, configuration validation, structured logging, and Docker Compose services.
- Added secret-validated, allowlisted private-chat webhook intake. Updates are committed before acknowledgment, deduplicated by Telegram update ID, and processed by separate interaction and delivery workers.
- Added transactional response enqueueing, concurrent worker claiming, rate-limit retry, and explicit uncertain-delivery handling after timeout or worker loss. Ambiguous Telegram sends are not automatically resent.
- Added async ASR, translation, TTS, and media interfaces with deterministic test doubles. Provisional settings use Whisper base, OPUS-MT, and Piper; production inference adapters and final model choices remain pending.
- Verification: 10 unit tests and 12 integration tests passed against disposable PostgreSQL 16, including concurrent duplicate intake, transaction rollback, access revocation, delivery recovery, and migration downgrade/upgrade and schema comparison. Ruff and Compose configuration validation passed.
- Unit tests required execution outside the restricted sandbox because its thread-notification restrictions stalled asyncio.to_thread; the unrestricted run passed. The initial PostgreSQL image download timed out; retry succeeded and integration tests completed.
- Remaining before M2 closes: build the application Docker image and smoke-test a fresh Compose deployment; finish operational documentation. No live Telegram webhook was registered. Current replies are foundation placeholders; sentence capture, collections, discovery, and reviews are later milestones.

### 2026-11-04 — Second server results inspected

- Recorded detailed findings in [second-run findings](docs/benchmark_findings_20261104.md). Both ASR models now complete; six-clip pooled WER is 15.4% for base and 7.7% for small. Two-minute warm medians are 45.41 and 50.47 seconds.
- Verified all 486 expanded-TTS WAV checksums and durations. Thorsten medium has no warnings and a 0.23-second warm median; four other Piper voices each emit 144 identical missing-phoneme warnings. Qwen completes but takes 9.5–10.6 minutes for the longest input on this CPU.
- Listening scores remain blank. M1 remains in progress pending quality/license approval and final voice selection. Preserve provisional application settings; stronger candidate recommendations are recorded without treating them as approved model choices.

### 2026-11-04 — Admin approval and external deployment revision

- Replaced environment allowlisting with persistent pending/approved/rejected/revoked users, a protected single admin, approval channel callbacks, preapproval by ID, paginated lists/counts, and an access-change audit trail. Repeated starts and stale buttons do not repeat requests/decisions.
- Replaced Telegram Pydantic wrappers with native telebot types and AsyncTeleBot handlers. Kept durable intake, transaction rollback, and multi-action outbox delivery; user status notifications remain deliverable after rejection/revocation.
- Adopted the uv Python 3.12 image, host-only 127.0.0.1:8060, stable Compose naming, and external bind-mounted database/configuration/runtime storage. Added environment setup and artifact migration tools plus the operations runbook.
- Existing local benchmark artifacts copied to `/home/abdulhade/language_bot_data`: 585 files verified, originals retained. Runtime manifests are relocated while historical reports retain original measurement paths.
- Verification in progress: initial revised suites passed 10 unit, 17 PostgreSQL integration, and 33 benchmark-tool tests. Additional storage/notification tests and final deployment/recreation checks follow before closing M2.

### 2026-11-04 — M2 revision verified and complete

- Final application checks: 13 unit tests and 19 real-PostgreSQL integration tests passed; benchmark regression suite: 33 tests passed. Ruff and whitespace checks passed. Default pytest discovery also works; database tests skip unless an explicit disposable test database is supplied.
- Built `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` application images. Fresh Compose deployment applied both migrations, started both workers, and returned ready at `127.0.0.1:8060`; PostgreSQL had no published host port.
- Rebuilt/recreated the isolated deployment from `/tmp/language-bot-relocated-checkout` with the same external data root. Approved user record, configuration checksum, and model marker survived. Confirmed worker startup and readiness, then stopped the test deployment.
- Installed the external development environment at `/home/abdulhade/language_bot_data/venvs/app`; cache reuse resolved stalled downloads. External `.env` contains setup placeholders, not live bot credentials. Original repository artifacts/environments remain as retained copies; new documented workflows use the external root.
- Live Telegram registration and channel delivery were not attempted with placeholder credentials. Follow [operations](docs/operations.md) for channel rights, configuration, nginx, and webhook registration. No M3 sentence features or new model experiments were started.

### 2026-11-04 — Standard Docker Compose commands

- Compose now defaults directly to `$HOME/language_bot_data` and loads the external `.env` for every service, including PostgreSQL. No wrapper, shell sourcing, or `--env-file` flag is required for ordinary deployment commands.
- `LANGUAGE_BOT_DATA_DIR` remains an optional shell override. Updated operational commands to plain `docker compose`; retained the old wrapper as a compatibility alias.

### 2026-11-04 — Direct bot API and durable model operations (in progress)

- Owner replaced raw-update persistence and generic interaction/delivery workers with direct API handling plus durable domain operations. Three model containers are implemented: Whisper small, OPUS-MT de→en, Piper Thorsten medium. Their queues live in PostgreSQL; model files and job artifacts remain outside the repo.
- Voice/text capture now advances through transcription (voice only), translation, and a final edit showing both texts. Owner explicitly deferred generated audio/playback in bot responses; TTS's internal API is implemented independently.
- Added an irreversible upgrade migration preserving user approvals, audit records, and channel references while removing old inbox/outbox payload tables. Backup instructions are in the operations guide.
- Verification underway: initial unit and integration suites passed; final migration, cleanup, image build, and service smoke checks are pending before completion is recorded.

### 2026-11-05 — Current deployment repaired and verified

- Inspected Compose logs: the external model directory was empty, causing ASR/translation local-model failures and a missing Piper configuration. Prepared all three pinned runtime models.
- Fixed the runtime downloader's file filter to include `.txt` files; Whisper small requires `vocabulary.txt`, which the previous filter omitted. Rebuilt the preparation image and fetched the missing vocabulary.
- Backed up the current database to `/tmp/language-bot-before-deployment-fix-20261105.dump` before restarting the stack; confirmed migration `0003_operations` was already applied. This local temporary backup is not an off-host recovery backup.
- Removed obsolete interaction/delivery worker containers using Compose's orphan cleanup. API, PostgreSQL, ASR, translation, and TTS are now healthy; migration exits successfully and the host API readiness endpoint returns ready.
- Real private-API smoke checks passed: German text translated to English, Piper produced a valid WAV, and Whisper transcribed that WAV as “Guten Morgen, ich lerne Deutsch.” Released smoke-job input/output artifacts through the model APIs.
- Verification: 13 unit tests and 27 integration tests against disposable PostgreSQL passed; downloader Ruff and whitespace checks passed. No live Telegram messages were sent. Fresh deployment/recreation and live Telegram acceptance for the latest architecture remain unverified, so M2 stays in Verification.

### 2026-11-05 — Owner accepts M2; M3 implemented and verified

- Owner confirmed live transcription and translation, accepted the remaining M2 verification as done, and authorized M3. Marked M2 complete by owner acceptance without claiming fresh-deployment/recreation checks were run.
- Added user-owned sentences, optional immediate-copy provenance, configured voices, shared media references, and sentence-owned audio assets. Normalized German has a non-unique index; intentional identical submissions remain separate entries. Active generated assets are unique per sentence/voice.
- Completed capture saves a sentence and any original Telegram file reference transactionally before the final message edit. Operation locking, claim-token fencing, and a unique capture identity prevent duplicate entries on recovery/retry. Archive uploads and generated playback remain later milestones.
- Added private-by-default personal `/library` pagination, `/entry ID`, and entry/collection buttons on completed status messages. Entry queries include the requesting owner; callbacks recheck approval and ownership.
- Duplicate checks use Unicode NFC and collapsed whitespace while preserving case and punctuation. Only the owner's entries and other users' public entries can produce warnings; no warning pauses processing. Simultaneous captures remain independent and warnings are best-effort.
- Migration `0004_collections` preserves approvals and completed captures, including original recording references. Missing legacy user records default to private/pending; migration never grants access. Imported results do not get retroactive warnings.
- Verification: 14 unit and 33 real-PostgreSQL integration tests passed, covering independent identical entries, private/public warnings, original media, concurrent submissions, fenced retries, and paginated owner-only commands/callbacks. Migration tests verify backfill and downgrade/re-upgrade preservation. Ruff and whitespace checks passed. Live deployment has not yet been upgraded to M3.

### 2026-11-05 — M3 deployment verified

- On resuming deployment work, discovered `0004_collections` was already applied. All three completed captures had saved entries, with one original recording asset; no completed captures were missing from the library. This supersedes the earlier deployment-pending note.
- Created a safety backup of the current database at `/tmp/language-bot-before-m3-20261105.dump`, rebuilt current images, and reconciled/recreated the stack with `docker compose up -d --remove-orphans --wait --wait-timeout 180`. This backup already contains the M3 schema.
- All five long-running services are healthy and migration exits successfully. `alembic check` reports no schema differences; host API readiness returns ready. Saved data remains present after recreation.
- Deployed `/library`, entry details, and ownership isolation passed smoke checks against existing saved data with Telegram replies captured locally. No test messages were sent to Telegram.
- Real translation, TTS WAV generation, and ASR transcription passed again through private HTTP services after recreation; smoke-job intermediate files were released through their APIs.

### 2026-11-05 — M3 committed; opt-in M4 archive implementation

- Committed M3 as `3f0508f`. Added `ArchiveJob` and migration `0005_archive_jobs`, backfilling original recordings and atomically enqueuing new originals with saved entries. Existing text/translation and Telegram file references survive storage failures.
- Added a server-side Telegram copy adapter that strips captions/keyboards and avoids forwarding links. Archive jobs pin their destination on first claim, use SKIP LOCKED leases and heartbeats, fence completion, honor rate-limit delays, and bound copy attempts. Uncertain outcomes and known orphan receipts are logged for cleanup without duplicating active assets.
- Added per-recording storage status and owner/approval-checked manual retries. Capture status rendering now reads persisted state and refuses expired claims before issuing obsolete progress edits. Existing model artifact cleanup remains in place; server-side original copies introduce no local audio upload files.
- Automatic approval review rejected activating a recording-copy worker without explicit destination/payload approval. Added `TELEGRAM_ARCHIVE_ENABLED=false` by default and tested the disabled gate. No recordings have been copied and the live M3 deployment has not been upgraded to this implementation.
- Read-only Telegram checks confirmed the configured destination is a channel without a public username and the bot is an administrator with posting rights. Activation and live archive validation await owner approval of copying original voice/audio messages, with captions removed, to that configured private channel.
- Verification: 17 unit and 40 disposable-PostgreSQL integration tests pass. New archive tests cover transactional rollback, concurrent claims, disabled behavior, slow network without held database locks, expired leases/late receipts, Retry-After, bounded uncertain retries, permanent rejection, owner-only retry callbacks, and final-attempt recovery. Migration tests verify archive backfill and downgrade/re-upgrade. Ruff and whitespace checks pass.

### 2026-11-05 — M4 deployed with archive copying disabled

- On resuming, found migration `0005_archive_jobs` and the M4 images already deployed. Read-only verification confirmed five saved sentences, three original-recording assets, and three pending archive jobs with zero attempts and no archive receipts.
- Deployed `alembic check` reports no schema differences and API readiness returns ready. The configured destination remains the previously inspected private channel; `TELEGRAM_ARCHIVE_ENABLED` remains false.
- Automatic approval review rejected the activation attempt because “Continue working” was not accepted as explicit approval of the recording payload and Telegram destination. No activation command ran, no external configuration was changed, and no recordings were copied. Live archive validation remains pending explicit owner approval.

### 2026-11-05 — Owner approves archive activation; M4 complete

- Owner explicitly approved copying existing and future original voice/audio recordings, with captions removed, into the configured private Telegram archive channel. Set `TELEGRAM_ARCHIVE_ENABLED=true` in the external configuration and recreated the stack successfully.
- Before activation, created permission-restricted database and configuration backups under the external data root: `backups/before-m4-activation-20261105T125142Z.dump` and `.env` with the same filename stem. Configuration contents and credentials were not printed.
- Four archive jobs, including a new recording queued during activation, completed in one attempt each. All four media records have receipts in the approved destination. Checks found no duplicate active original-media assets or duplicate archive message receipts; no upload errors appeared in fresh API logs.
- All five long-running services are healthy, migration exits successfully, `alembic check` reports no schema differences, and API readiness returns ready. The enabled flag and configured destination were checked inside the running API.
- M4 acceptance is supported by the previous 17 unit/40 PostgreSQL integration results plus successful live archive copies. Original-recording playback controls and bot-generated TTS remain M5 follow-up work rather than claims of completed playback.

### 2026-11-05 — M5 playback and opt-in generated speech implementation

- Continued into playback/TTS after owner requested the next step. Added owner/approval-checked archived original playback and generated audio controls. Existing entries are never automatically synthesized; Generate German audio explicitly creates one asset/job per sentence and provisional Thorsten voice.
- Added migration `0006_audio_jobs`, durable synthesis/upload stages, stable model identities, per-stage retries, revision/claim fencing, and cleanup of successful model audio after archive persistence. Upload failure retains successful synthesis and text/translation; expired model audio is regenerated with a new request identity.
- TTS handles up to 10,000-character transcripts by synthesizing 1,000-character chunks into one valid WAV. Added optional in-memory MP3 encoding through the private model API so Telegram's audio player receives the supported format. The original WAV API remains compatible.
- Generated uploads and playback reuse the private archive, strip captions, and use a generic title/voice label without contributor attribution. Capture result buttons can be refreshed after successful generation without creating another processing-status message.
- Automatic approval review rejected generated uploads as a different payload from the owner's original-recording approval. Added `TELEGRAM_TTS_ENABLED=false` by default; no generated audio has been uploaded to Telegram. Activation awaits explicit approval of generated audio from saved German text and the private channel destination.
- Verification: 22 unit and 48 disposable-PostgreSQL integration tests pass. Coverage includes opt-in gating, concurrent request deduplication, owner/approval checks, archived playback reuse, upload retries preserving synthesis, failed-synthesis manual retry, expired intermediate audio, revision/claim fencing, and private model HTTP download/cleanup. Ruff and whitespace checks pass; application/model images build successfully.
- Real isolated Piper validation synthesized two chunks into an 85.51-second WAV, encoded a 684,717-byte MP3, decoded the same duration, and removed temporary output. This used generic German test text and sent nothing to Telegram.
- Backed up the live database to `backups/before-m5-20261105T131012Z.dump` under the external data root and deployed migration `0006_audio_jobs` with generated uploads verified disabled. All five long-running services are healthy and deployed `alembic check` reports no differences.
- Deployed original entry/playback callbacks and ownership checks passed with replies captured locally. The deployed private MP3 endpoint returned 15,213 bytes for generic test speech, then released its WAV via DELETE. No generated audio jobs/assets or Telegram uploads exist. Original playback is available through `/library`; live generated-audio upload/playback validation remains pending activation approval.

### 2026-11-05 — Owner approves generated audio; M5 complete

- Owner explicitly approved enabling on-demand Thorsten TTS, storing generated MP3s from saved German text in the configured private archive, and owner-only playback. Enabled `TELEGRAM_TTS_ENABLED=true` in external configuration and recreated only the API; all model services remained available.
- Before activation, created permission-restricted database/configuration backups under the external data root: `backups/before-tts-activation-20261105T143741Z.dump` and `.env` with the same filename stem. No credentials/configuration contents were printed.
- Requested audio for one existing administrator-owned entry. Live synthesis/upload completed successfully, the archive receipt was saved, and the 6,765-byte MP3 was read back from Telegram using its stored file reference. The temporary model WAV was released after persistence.
- Owner-only playback queries and the enabled Play German audio entry/callback controls passed verification with playback effects captured locally. No extra playback message was sent during this check. Original and generated audio playback use archived references rather than repeating inference.
- All five long-running services are healthy, API readiness returns ready, and deployed `alembic check` reports no schema differences. Together with the recorded 22 unit/48 PostgreSQL tests and long-text real-codec checks, this completes M5's current capture/playback scope. M7 will extend validation to public discovery controls and its multi-user scenarios.

### 2026-11-05 — Generated-audio archive labels

- Owner requested useful generated-audio labels in the private storage channel. New uploads use the German sentence as title, the voice name as performer, and a readable voice-based filename. Archive captions contain German text, voice/provider, sentence ID, owner user ID, revision, and audio asset ID.
- Caption rendering respects Telegram's 1,024-character limit conservatively in UTF-16 units, shortening German text where necessary while retaining metadata. User IDs remain confined to archive captions, which playback removes; contributor IDs are not placed in audio titles, performer names, or filenames.
- Verification: 23 unit and 48 PostgreSQL integration tests passed, covering upload metadata, long Unicode captions retaining identifiers, and caption-free owner playback. Built and deployed the API update; readiness is healthy. Updated captions on both existing generated-audio archive posts without regenerating or reuploading recordings. Existing audio titles are unchanged; new uploads receive the sentence/voice title fields.

### 2026-11-05 — Correct initial uploads, German filenames, and HTML archive metadata

- Owner reported new uploads still lacked the expected metadata and requested sentence-based audio names plus HTML German/English captions and hashtagged metadata. The previous live verification only checked manually edited captions, so it did not validate the running upload path.
- Found the actual deployment defect: `/app/app` had current source but the non-editable application installed under site-packages was an older uv-cached wheel. Console entry points used that stale wheel, while `python -c` inspections accidentally imported newer working-directory source. Reproduced mismatched installed/source module hashes and missing HTML metadata code in the installed package.
- Docker now forces reinstalling the local application in both API/base and inference stages and fails the build if any installed Python module differs from source. Rebuilt and recreated all services. Verified with `python -P` that installed modules match source and are loaded from site-packages.
- Audio titles and sanitized MP3 filenames now use the German sentence. HTML captions contain escaped German and English sections and searchable provider, voice, sentence, owner, audio, and revision hashtags. UUID tags omit hyphens to remain whole searchable hashtags. Caption budgets account for both languages after HTML parsing, retaining identifiers when text is shortened.
- Updated six older generated archive posts in place, including their filenames/titles, preserving message IDs and active asset references. Their existing MP3 bytes were reused without TTS regeneration; stored file references were updated from Telegram's returned messages.
- Verified a fresh end-to-end upload after the cache fix: archive message 15 returned the exact expected caption, hashtags, title, and filename on initial send. Its structured metadata-confirmation event reports all checks true. No manual caption repair was needed for this fresh post.
- Verification: 24 unit and 48 PostgreSQL integration tests pass, including HTML escaping, long multilingual/Unicode captions, metadata on the worker upload path, sentence-based filenames, and caption-free owner playback. Docker source/install consistency checks pass. All services are healthy.
