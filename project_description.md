# German Learning Telegram Bot

## 1. Project Goal

Build a private Telegram bot that acts as a personal German sentence-learning database.

The user should be able to send German sentences either as:

- Telegram voice notes
- Plain German text

The bot processes each sentence into a reusable learning record containing:

- Original user voice note, when available
- Canonical German text
- English translation
- Multiple generated German TTS recordings using different voices
- Review/testing metadata

The important architectural principle is:

> The sentence database is the core product. Telegram is only the interface.

The application should later support many learning modes using the same stored data, including listening tests, translation tests, speaking tests, flashcard-style review, spaced repetition, random review, and AI-generated enrichment.

The initial version should NOT depend on a general-purpose LLM.

---

# 2. Core Processing Pipeline

For a German voice note:

```text
Telegram Voice Note
        ↓
Archive original audio
        ↓
ASR / Speech-to-Text
        ↓
German sentence
        ↓
German → English translation
        ↓
Generate multiple German TTS audios
        ↓
Archive generated audios
        ↓
READY
```

For German text:

```text
German text
        ↓
German → English translation
        ↓
Generate multiple German TTS audios
        ↓
Archive generated audios
        ↓
READY
```

The initial AI stack should conceptually be:

```text
ASR:
local speech recognition model
e.g. faster-whisper

Translation:
local machine translation model
e.g. OPUS-MT German → English

TTS:
local TTS provider
e.g. Qwen3-TTS / another local provider
```

These are only default implementations.

Application logic MUST NOT directly depend on these models.

---

# 3. Critical Architecture Requirement: Model Abstraction

Every AI/model capability must sit behind an interface.

The application must not contain code like:

```python
whisper_model.transcribe(...)
qwen.generate(...)
opus.translate(...)
```

inside Telegram handlers or business logic.

Instead define abstractions conceptually similar to:

```python
class ASRProvider(Protocol):
    async def transcribe(
        self,
        audio_path: Path,
        language: str = "de",
    ) -> TranscriptionResult:
        ...
```

```python
class TranslationProvider(Protocol):
    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> TranslationResult:
        ...
```

```python
class TTSProvider(Protocol):
    async def synthesize(
        self,
        text: str,
        voice: VoiceConfig,
    ) -> GeneratedAudio:
        ...
```

The concrete implementations can initially be:

```text
FasterWhisperASRProvider

OpusMTTranslationProvider

QwenTTSProvider
```

But changing models later should require replacing/configuring providers rather than modifying application logic.

For example:

```text
ASR_PROVIDER=faster_whisper

TRANSLATION_PROVIDER=opus_mt

TTS_PROVIDER=qwen3_tts
```

Future implementations might include:

```text
OpenAIASRProvider
GoogleTranslationProvider
ElevenLabsTTSProvider
AnotherLocalTTSProvider
```

without changing Telegram handlers, database logic, review logic, or sentence management.

---

# 4. Telegram as Audio Storage

Do NOT store audio blobs inside PostgreSQL.

Do NOT persist generated audio files locally after they have been successfully archived.

Create a private Telegram channel dedicated to media storage.

The bot must be an administrator of this channel.

Example configuration:

```text
TELEGRAM_MEDIA_CHANNEL_ID=-100xxxxxxxxxx
```

Every permanent audio asset should be copied/uploaded into this private archive channel.

This includes:

```text
Original user recordings

Generated TTS voice 1

Generated TTS voice 2

Generated TTS voice 3

Any future regenerated recordings
```

The database stores references to Telegram media rather than binary data.

Store enough information to retrieve/re-send the audio reliably, for example:

```text
telegram_channel_id
telegram_message_id
telegram_file_id
telegram_file_unique_id
```

When the bot needs to send an existing recording to the user, it should reuse the stored Telegram reference rather than regenerating or re-uploading the audio.

Prefer copying/sending the archived Telegram media directly.

---

# 5. Temporary Files

Local AI models will occasionally require real audio files.

Temporary local storage is therefore allowed.

Example:

```text
Telegram voice
    ↓
download to /tmp/germanbot/...
    ↓
ASR
    ↓
delete temporary file
```

TTS works similarly:

```text
TTS
 ↓
temporary .wav/.ogg
 ↓
upload to private Telegram archive
 ↓
store Telegram reference
 ↓
delete local temporary file
```

Temporary files should be automatically cleaned up even if processing fails.

The application must not rely on local generated files for permanent storage.

Telegram is the permanent media store.

---

# 6. Voice Note User Experience

When the user sends a German voice note, the bot should immediately create ONE processing-status message.

Do not send five separate status messages.

The same Telegram message should continually be edited as processing progresses.

Example initial state:

```text
🇩🇪 Processing sentence

🎙 Transcribing...
```

After transcription:

```text
🇩🇪 Processing sentence

✅ Transcription

Ich muss morgen früh arbeiten.

🌍 Translating...
```

After translation:

```text
🇩🇪 Processing sentence

✅ German

Ich muss morgen früh arbeiten.

✅ English

I have to work early tomorrow.

🔊 Generating voices...

Voice 1 ⏳
Voice 2 ⏳
Voice 3 ⏳
```

As generation progresses:

```text
🇩🇪 Processing sentence

✅ German

Ich muss morgen früh arbeiten.

✅ English

I have to work early tomorrow.

🔊 Generated voices

Voice 1 ✅
Voice 2 ✅
Voice 3 ⏳
```

Final state:

```text
🇩🇪 Sentence saved

German:
Ich muss morgen früh arbeiten.

English:
I have to work early tomorrow.

🎙 Original recording ✅

🔊 Generated voices
1. ✅
2. ✅
3. ✅

Status: READY
```

The final message should contain inline controls for editing/managing the record.

---

# 7. Text Sentence User Experience

If the user sends plain German text, skip transcription.

Example:

```text
🇩🇪 Processing sentence

German:
Ich habe heute keine Zeit.

🌍 Translating...
```

Then:

```text
🇩🇪 Sentence saved

German:
Ich habe heute keine Zeit.

English:
I don't have time today.

🔊 Generated voices
1. ✅
2. ✅
3. ✅

Status: READY
```

There is no original recording for text-only records.

---

# 8. Record Controls

After processing finishes, the status message should become the management interface for that sentence.

Use Telegram inline keyboard buttons.

Conceptually:

```text
[ ✏️ German ] [ ✏️ English ]

[ 🎙 Replace original audio ]

[ 🔊 Audios ]

[ ♻️ Regenerate voices ]

[ 🗑 Delete record ]
```

Opening the audio management section could show:

```text
Generated audios

Voice 1 ✅
Voice 2 ✅
Voice 3 ✅

[ ▶️ 1 ] [ 🗑 1 ]
[ ▶️ 2 ] [ 🗑 2 ]
[ ▶️ 3 ] [ 🗑 3 ]

[ ♻️ Regenerate all ]
[ ➕ Generate missing ]
[ ◀️ Back ]
```

Exact button layout may be adjusted to Telegram constraints.

---

# 9. Editing German Text

The German text is the canonical sentence.

If the user chooses:

```text
✏️ German
```

the bot should enter a temporary interaction state such as:

```text
Send the corrected German sentence.

Current:

Ich muss morgen arbeiten.
```

The next text message from the user becomes the replacement German text.

Example:

```text
Ich muss morgen früh arbeiten.
```

Changing the canonical German sentence invalidates everything derived from it.

Dependency graph:

```text
German sentence
     │
     ├── English translation
     │
     └── Generated TTS audios
```

Therefore:

```text
German changed

→ translation becomes stale
→ generated audios become stale
```

The bot should automatically re-run:

```text
translation
+
TTS generation
```

unless the architecture later supports explicit manual overrides.

The original user voice recording does not need to be deleted automatically.

---

# 10. Editing English Translation

The user should be able to manually correct the translation.

Button:

```text
✏️ English
```

Bot:

```text
Send the corrected English translation.

Current:

I have to work tomorrow.
```

Changing the English translation affects nothing else.

It must NOT regenerate TTS.

Dependency:

```text
English translation

does NOT affect

German audio
```

---

# 11. Replacing Original User Audio

For records created from voice notes, allow:

```text
🎙 Replace original audio
```

The bot enters a state waiting for another Telegram voice message.

Example:

```text
Send the replacement German voice note.

Cancel with /cancel.
```

When received:

1. Archive the new audio in the private Telegram media channel.
2. Replace the record's original-audio reference.
3. Run ASR again.
4. Replace the canonical German transcription.
5. Re-run translation.
6. Regenerate generated TTS voices.
7. Remove obsolete generated audio references.
8. Optionally delete obsolete archive messages after the replacement succeeds.

The operation should preferably behave transactionally:

Do not destroy the old working record until the replacement pipeline has succeeded far enough that the new record is usable.

---

# 12. Audio Regeneration

The user should be able to regenerate generated TTS recordings.

Actions:

```text
Regenerate all voices

Regenerate voice 1

Regenerate voice 2

Regenerate voice 3

Generate missing voices
```

Regeneration should:

```text
canonical German text
        ↓
selected TTS provider
        ↓
temporary generated file
        ↓
Telegram archive
        ↓
database audio reference
```

When replacing an existing generated audio:

1. Generate the new audio.
2. Upload/archive successfully.
3. Update the database.
4. Delete the previous archive message if appropriate.

Never delete the old asset before the new asset has been safely created.

---

# 13. Deleting Audio

Every audio asset must be independently deletable.

Audio types include:

```text
ORIGINAL

GENERATED
```

Deleting an audio asset should:

1. Delete its message from the private archive channel if possible.
2. Remove its database reference.
3. Update the record UI.

Deleting generated audio must NOT delete the sentence.

Example state:

```text
Voice 1 ✅
Voice 2 ❌ missing
Voice 3 ✅
```

The user can later choose:

```text
Generate missing
```

---

# 14. Deleting a Whole Record

The user should be able to permanently delete a sentence.

Require confirmation:

```text
Delete this sentence permanently?

[ Yes, delete ] [ Cancel ]
```

Deletion should attempt to delete:

```text
original archived audio
all generated archived audios
all review statistics
all related metadata
sentence record
```

The database should not retain orphan audio records.

If deleting Telegram media fails, database deletion may continue but the failure should be logged.

---

# 15. Processing State Machine

Do not represent processing with random booleans.

Use an explicit state.

Suggested states:

```text
CREATED

ARCHIVING_SOURCE

TRANSCRIBING

TRANSCRIBED

TRANSLATING

TRANSLATED

GENERATING_AUDIO

READY

FAILED

DELETING
```

Individual generated audios may also have states:

```text
PENDING

GENERATING

READY

FAILED

DELETED
```

This allows the Telegram UI to accurately describe current work.

---

# 16. Failure Handling

Failure of one step should not destroy the entire record.

Example:

```text
German:
Ich muss morgen arbeiten.

English:
I have to work tomorrow.

Generated voices:

Voice 1 ✅
Voice 2 ❌
Voice 3 ✅
```

Final UI:

```text
⚠️ Sentence saved with errors

Voice 2 failed to generate.

[ Retry voice 2 ]
```

Likewise if translation fails:

```text
✅ Transcription complete

Ich muss morgen arbeiten.

❌ Translation failed

[ Retry translation ]
```

The bot should allow retrying individual failed steps.

---

# 17. Concurrency and User Interaction

Long-running model inference must not block Telegram updates.

Telegram handlers should remain responsive.

Processing should be separated from bot update handling.

Conceptually:

```text
Telegram Handler
       ↓
create/update DB record
       ↓
enqueue work
       ↓
background worker
       ↓
ASR / Translation / TTS
```

A queue-backed worker architecture is recommended.

Redis may be used for job processing.

The implementation should make it possible to run:

```text
bot process

worker process

model services
```

separately.

However, keep the initial implementation reasonably simple.

---

# 18. Database Model

Suggested conceptual model.

## Sentence

```text
id

german_text

english_text

source_type
    TEXT
    VOICE

status

created_at
updated_at
```

Possible additional fields:

```text
translation_provider

translation_model

asr_provider

asr_model
```

These are useful for knowing how a record was produced.

---

## AudioAsset

```text
id

sentence_id

type
    ORIGINAL
    GENERATED

voice_id
nullable for ORIGINAL

provider

model

telegram_channel_id

telegram_message_id

telegram_file_id

telegram_file_unique_id

status

created_at
updated_at
```

Do not create columns such as:

```text
audio_1
audio_2
audio_3
```

Audio must be a one-to-many relationship.

This allows a sentence to eventually contain:

```text
3 voices
5 voices
slow recordings
fast recordings
Austrian recordings
personal recordings
etc.
```

without changing the schema.

---

## Voice

TTS voices should also be data-driven.

Example:

```text
id

name

provider_voice_id

provider

language

enabled

settings JSON
```

Example records:

```text
anna
lukas
sophie
```

The sentence-generation service can select three enabled German voices.

Do not hard-code model-specific voice IDs throughout the application.

---

# 19. TTS Voice Selection

Initially use three configured German voices.

Example:

```text
GERMAN_TTS_VOICES=anna,lukas,sophie
```

Every newly created sentence receives one generated recording for each enabled default voice.

The architecture should later allow:

```text
normal voice

slow voice

natural/conversational voice

German regional voices

Austrian voices

Swiss German voices
```

without restructuring the database.

---

# 20. Media Storage Abstraction

Telegram media storage should also be abstracted.

Define something conceptually like:

```python
class MediaStore(Protocol):

    async def save_user_audio(...) -> StoredMedia:
        ...

    async def save_generated_audio(...) -> StoredMedia:
        ...

    async def send_to_user(...) -> None:
        ...

    async def delete(...) -> None:
        ...
```

Initial implementation:

```text
TelegramChannelMediaStore
```

This prevents the entire application from becoming tightly coupled to Telegram storage.

A future implementation could use:

```text
S3MediaStore
LocalMediaStore
MinioMediaStore
```

without changing sentence logic.

---

# 21. Service Layer

Telegram handlers must NOT contain business logic.

Use services such as:

```text
SentenceService

AudioService

ProcessingService

ReviewService

MediaService
```

Example:

```text
Telegram callback
      ↓
SentenceService.update_german(...)
      ↓
invalidate translation
invalidate generated audio
      ↓
ProcessingService.reprocess(...)
```

The Telegram layer should mostly translate Telegram events into application service calls.

---

# 22. Suggested Project Structure

Conceptually:

```text
app/

    bot/
        handlers/
        callbacks/
        keyboards/
        states/
        message_renderer.py

    domain/
        sentence.py
        audio.py
        voice.py
        review.py
        enums.py

    services/
        sentence_service.py
        processing_service.py
        audio_service.py
        review_service.py

    providers/
        asr/
            base.py
            faster_whisper.py

        translation/
            base.py
            opus_mt.py

        tts/
            base.py
            qwen_tts.py

        media/
            base.py
            telegram_channel.py

    db/
        models/
        repositories/
        migrations/

    workers/
        tasks.py

    config.py
    main.py
```

Provider-specific code must stay under:

```text
providers/
```

Model details must not leak into domain or Telegram code.

---

# 23. Processing Message Renderer

Create a dedicated component responsible for generating the editable Telegram status message.

Do not build message text independently inside every processing step.

Conceptually:

```python
render_sentence_status(sentence)
```

It should generate output based on current database state.

Example:

```text
TRANSCRIBING
TRANSLATING
GENERATING_AUDIO
READY
FAILED
```

Every worker update should modify the database state and then ask the renderer to refresh the Telegram status message.

This makes crash recovery and retries much easier because UI state comes from persisted state rather than in-memory progress.

---

# 24. Learning Engine

The architecture should support future tests, but AI is NOT required for the learning engine.

From:

```text
German

English

Original audio

Generated audio A

Generated audio B

Generated audio C
```

the application can already support:

### German → English

Show:

```text
Ich muss morgen arbeiten.
```

Ask for the English meaning.

---

### English → German

Show:

```text
I have to work tomorrow.
```

Ask for German.

---

### Listening Dictation

Choose a random audio asset:

```text
Voice A
Voice B
Voice C
Original
```

Send only the audio.

Ask the user to type what they heard.

---

### Listening Meaning

Send random German audio.

Ask for the English meaning.

---

### Speaking

Show:

```text
I have to work tomorrow.
```

Ask the user to send a German voice note.

Run ASR and compare the resulting German text with the expected German sentence.

---

# 25. Review Metadata

Design the schema so spaced repetition can later be implemented.

Possible review information:

```text
sentence_id

times_seen

correct_count

wrong_count

last_reviewed_at

next_review_at

interval

difficulty
```

Do not make review scheduling dependent on AI.

---

# 26. Future AI Enrichment

General-purpose LLM functionality should NOT be required for creating a sentence.

Later an optional enrichment provider may add:

```text
CEFR level

grammar explanation

important vocabulary

tags

three related example sentences

alternative phrasing

common mistakes

grammar categories
```

This should be asynchronous and optional.

Conceptually:

```text
Sentence
   ↓
READY

   ↓ optional
EnrichmentProvider

   ↓
enriched metadata
```

If enrichment is disabled or unavailable:

```text
the application must continue working normally
```

Possible future interface:

```python
class SentenceEnrichmentProvider(Protocol):

    async def enrich(
        self,
        german_text: str,
        english_text: str,
    ) -> SentenceEnrichment:
        ...
```

---

# 27. Dependency Invalidation Rules

Derived data must never silently become stale.

Use these rules:

```text
ORIGINAL AUDIO changed
        ↓
re-transcribe
        ↓
GERMAN changes
        ↓
invalidate translation
invalidate generated TTS
```

```text
GERMAN changed manually
        ↓
invalidate translation
invalidate generated TTS
```

```text
ENGLISH changed manually
        ↓
nothing else invalidated
```

```text
GENERATED AUDIO changed
        ↓
nothing else invalidated
```

This logic belongs in the domain/service layer, not Telegram handlers.

---

# 28. User Interaction State

Some bot commands require waiting for the next user message.

Examples:

```text
editing German text

editing English text

replacing original recording
```

Use explicit short-lived conversation states.

Example:

```text
IDLE

AWAITING_GERMAN_EDIT

AWAITING_ENGLISH_EDIT

AWAITING_REPLACEMENT_AUDIO
```

Support:

```text
/cancel
```

to leave the current interaction state.

---

# 29. Idempotency

Telegram updates and worker jobs may be delivered/retried.

Processing operations should therefore be designed to be idempotent where practical.

For example:

A retry of:

```text
generate voice 2
```

should not accidentally create ten active Voice-2 records.

There should be one currently active generated audio asset for:

```text
sentence + configured voice
```

unless historical versioning is deliberately implemented later.

---

# 30. Logging

Log important events with structured information.

Examples:

```text
sentence_created

source_audio_archived

asr_started

asr_completed

translation_started

translation_completed

tts_started

tts_completed

tts_failed

sentence_edited

audio_regenerated

record_deleted
```

Include IDs rather than dumping large content unnecessarily.

Model/provider failures should be logged with enough context to debug them.

---

# 31. Security

The bot is initially intended for one private user.

Implement an allowed Telegram user ID configuration.

Example:

```text
ALLOWED_TELEGRAM_USER_IDS=123456789
```

Ignore or reject messages from unauthorized users.

The private archive channel must never be exposed publicly.

Secrets must come from environment/configuration.

Never commit:

```text
Telegram bot token

channel IDs if considered sensitive

API keys

credentials
```

to source control.

---

# 32. Configuration

Configuration should include concepts such as:

```text
TELEGRAM_BOT_TOKEN

TELEGRAM_MEDIA_CHANNEL_ID

ALLOWED_TELEGRAM_USER_IDS

DATABASE_URL

REDIS_URL

ASR_PROVIDER

ASR_MODEL

TRANSLATION_PROVIDER

TRANSLATION_MODEL

TTS_PROVIDER

TTS_MODEL

GERMAN_TTS_VOICES
```

Provider-specific configuration may exist but should remain isolated.

---

# 33. Initial Scope

Implement the project incrementally.

The first important milestone is:

```text
User sends German voice
        ↓
editable progress message
        ↓
voice archived
        ↓
transcription
        ↓
translation
        ↓
3 generated voices
        ↓
all audio archived in Telegram
        ↓
READY record
        ↓
editable management controls
```

Then implement:

```text
text input

manual German edit

manual translation edit

replace source recording

regenerate audio

delete individual audio

delete whole record
```

Only after the sentence-management system is reliable should the review/testing engine be added.

Do NOT begin by implementing AI enrichment.

---

# 34. Definition of Done for Initial Sentence Pipeline

The initial sentence pipeline is considered complete when all of the following work reliably:

1. A German Telegram voice note can be submitted.

2. The original voice note is archived to the configured private Telegram media channel.

3. One status message is created and edited throughout processing.

4. The status visibly moves through transcription, translation, and TTS generation.

5. German transcription is persisted.

6. English translation is persisted.

7. Three different TTS recordings are generated.

8. Generated recordings are archived to the Telegram media channel.

9. No audio blob is stored in PostgreSQL.

10. Persistent local audio files are not required.

11. Temporary files are cleaned up.

12. Archived audio can later be sent again without regenerating it.

13. German text can be manually changed.

14. Changing German invalidates and regenerates translation and generated audio.

15. English translation can be manually changed without affecting audio.

16. Original user audio can be replaced.

17. Any generated audio can be regenerated individually.

18. Any audio asset can be deleted individually.

19. The whole sentence record can be deleted.

20. Partial processing failures can be retried.

21. AI/model implementations are hidden behind provider interfaces.

22. Telegram media storage is hidden behind a media-storage interface.

23. Telegram handlers contain minimal business logic.

24. The system is structured so future review/testing features can use the sentence database without depending on specific AI models.

---

# 35. Engineering Principles

Keep the implementation simple, typed, modular, and testable.

Prefer:

```text
clear domain objects

explicit state machines

dependency injection

small provider interfaces

database-backed state

idempotent jobs

reusable service methods
```

Avoid:

```text
model-specific calls inside Telegram handlers

huge Telegram handler files

audio blobs in the database

hard-coded voice IDs scattered across the codebase

business logic encoded in inline-button callbacks

processing progress stored only in memory

premature LLM integration

premature microservices
```

The architecture should be modular without becoming unnecessarily complicated.

The goal is a solid personal application that can grow organically.

---

# 36. Product Philosophy

The project should treat each saved German sentence as a reusable learning object:

```text
Sentence
├── German
├── English
├── original user pronunciation
├── generated pronunciation A
├── generated pronunciation B
├── generated pronunciation C
├── future review statistics
└── future enrichment
```

The application should make capturing sentences effortless first.

Everything else—testing, spaced repetition, grammar analysis, vocabulary extraction, and AI-generated examples—should build on top of this reliable sentence base.