# German Learning Bot: Implementation and Verification Plan

This document is the implementation source of truth agreed during planning. Where it differs from `project_description.md`, the decisions recorded here take precedence.

## Progress tracking

Move each milestone through **Not started → In progress → Verification → Done**. Check its checkbox only after its acceptance checks pass, and record test evidence and relevant changes in this table. Record any later agreed changes in the decision log at the end of this document.

| Milestone | Status | Evidence / notes |
|---|---|---|
| M0 — Specification and tracking | Not started | |
| M1 — Runtime and model feasibility | Not started | |
| M2 — Async application foundation | Not started | |
| M3 — Collections and duplicate warnings | Not started | |
| M4 — Durable jobs and archive storage | Not started | |
| M5 — Voice and text capture | Not started | |
| M6 — Editing and deletion | Not started | |
| M7 — Privacy, discovery, and copying | Not started | |
| M8 — Practice and FSRS | Not started | |
| M9 — Reminders and recovery | Not started | |
| M10 — Validation and deployment | Not started | |

## 1. Agreed product behavior

The first release supports one or two allowlisted users, with independent sentence collections, public discovery, audio management, and spaced repetition.

| Area | Agreed behavior |
|---|---|
| Access | Administrator-managed Telegram user allowlist |
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

Run these separate processes:

- **API:** receive authenticated Telegram webhooks and provide health/readiness endpoints.
- **Interaction worker:** process durable incoming updates, handle commands and callbacks, and send UI responses.
- **Processing worker:** execute durable sentence-processing jobs, with one CPU inference operation at a time.
- **Maintenance worker:** deliver reminders, refresh pending status messages, recover expired jobs, and clean temporary files.
- **PostgreSQL:** store application data, interaction state, jobs, and delivery work.

Use async database and Telegram I/O. Run blocking model inference outside the event loop in a bounded executor owned by the processing worker. Configure model thread counts and memory usage from benchmarks.

Each concurrent task gets its own SQLAlchemy session; transactions remain short and never span model inference or Telegram requests. This follows [SQLAlchemy’s async session guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html). Heavy computation belongs in separate workers rather than request-local background tasks, consistent with [FastAPI’s guidance](https://fastapi.tiangolo.com/tutorial/background-tasks/).

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
| `IncomingUpdate` / `Job` | Deduplicated Telegram updates and durable background work |

Each `Sentence` belongs directly to one user; there is no separate `UserSentence` model or association table. Audio assets, review cards, and entry-specific jobs reference `Sentence.id`. Copying creates a new `Sentence` owned by the recipient, with independent text, metadata, and review state; generated media can still be shared through `StoredMedia` references.

`copied_from_sentence_id` is an indexed, nullable foreign key to `Sentence.id`. Set it to the immediate source when copying, including when copying a copy; ordinary submissions leave it null. Keep the reference after personal edits. Use `ON DELETE SET NULL` so deleting a source preserves its copies. The reference records provenance only: it never synchronizes content, grants access to a private source, or exposes its owner. Future source-change notifications may use this relationship, subject to current visibility checks; implementing notifications is outside the initial release.

Database constraints enforce:

- No global or per-user uniqueness constraint on German text or its normalized lookup value. Each intentional submission or copy creates a separate user-owned `Sentence` record.
- One active generated asset per personal entry and configured voice.
- One review card per personal entry and mode.
- Unique processing identities for Telegram updates and retryable work.

For duplicate detection, normalize German using Unicode NFC, trim leading/trailing whitespace, and collapse internal whitespace. Preserve capitalization, punctuation, umlauts, and spelling. Index the lookup value without a uniqueness constraint.

Duplicate warnings appear in the existing status/detail message without confirmation or a processing pause. Check submissions, copies, German edits, and replacement-audio transcriptions; exclude the current entry from its own comparison. Never reveal or signal matches in another user’s private collection. Concurrent submissions may both proceed; detection is best-effort, not a locking or rejection mechanism.

Intentional duplicate content is distinct from delivery retries: processing the same Telegram update or retrying the same job must still produce only one logical operation.

### Interfaces and ownership rules

Provider interfaces cover ASR, translation, TTS, media storage, and review scheduling. Pydantic models validate configuration and service inputs/outputs. Model-specific types stay inside provider adapters.

Services handle sentence management, copying, discovery, audio, processing, privacy, and reviews. Telegram handlers only interpret updates and call these services.

Copies initially reference immutable generated media. Regeneration changes only the requesting entry’s asset reference. Delete archived media only after no active asset references it.

All public reads—including playback and callbacks from old discovery messages—recheck current visibility. Send archived media without forwarding contributor or archive-channel identity.

## 3. Trackable implementation milestones

Each milestone closes only when its acceptance checks pass. Implement milestones in order; milestone 1 is a deliberate feasibility gate before choosing model packages.

### [ ] M0 — Lock the specification and project tracking

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

### [ ] M2 — Establish the async application foundation

**Deliverables**

- Docker Compose services, dependency lockfile, environment configuration, and Alembic migrations.
- Async SQLAlchemy session management and provider dependency injection.
- FastAPI health/readiness endpoints and secret-validated Telegram webhook.
- Allowlist enforcement and durable incoming-update storage.
- Structured logs with user, entry, job, and operation IDs.

**Acceptance**

- A fresh deployment migrates and starts successfully.
- Invalid webhook secrets and unauthorized users cannot create application work.
- Repeated delivery of one Telegram update produces one logical operation.
- Health distinguishes a running process from a database-ready application.

### [ ] M3 — Implement personal collections and duplicate warnings

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

### [ ] M4 — Build durable jobs and archive storage

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

### [ ] M5 — Deliver voice and text capture

**Deliverables**

- Voice flow: archive → transcribe → normalize/check for duplicates → translate → generate three voices → archive.
- Text flow: normalize/check for duplicates → translate → generate three voices → archive.
- One editable processing-status message with management controls.
- Step-specific failure information and retry actions.
- Maximum input length/duration enforcement.

**Acceptance**

- Both input types produce persisted German, English, and three playable generated recordings.
- Voice entries also retain their original recording.
- Playback reuses Telegram references.
- A failed voice does not discard successful translation or other voices.
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
- **PostgreSQL integration tests:** concurrent intentional duplicate creation versus idempotent delivery retries, privacy-filtered duplicate lookup, job leasing, retries, revision conflicts, media references, and transactional enqueueing. Use real PostgreSQL rather than SQLite.
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
