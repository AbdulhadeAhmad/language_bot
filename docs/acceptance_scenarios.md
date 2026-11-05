# Acceptance scenarios — M0 baseline

These scenarios translate `implementation_plan.md` into observable outcomes. They are specifications, not claims that application tests already pass. Alice and Bob are approved users; Eve has no approved access. Each scenario starts from isolated data unless stated otherwise.

| ID | Milestone | Given / action | Required outcome |
|---|---|---|---|
| A01 | M2 | Eve sends text, voice, or a callback; a request has an invalid webhook secret. | No sentence, media operation, or model job is created. |
| A02 | M2, M4 | Telegram redelivers Alice's same update; a worker retries its job. | One logical operation, status message, and set of active assets. |
| A03 | M3 | Alice intentionally sends identical German twice in different updates; Bob sends it too. | Three independently owned sentences. Detected accessible matches produce a small warning, never rejection or automatic merging. |
| A04 | M3 | Compare `Ich  komme.` and ` Ich komme. `, composed/decomposed umlauts, `ich komme.`, and `Ich komme!`. | Whitespace and Unicode-equivalent forms match; case and punctuation differences do not. |
| A05 | M3, M7 | Bob privately saves text that Alice later submits. | Alice gets no indication that Bob's private sentence exists. |
| A06 | M3, M5 | Alice submits text; Bob submits a voice note. | Independent saved German/English entries and one edited progress message each, with advisory duplicate warnings where applicable. Bot audio and archive integration are deferred. |
| A07 | M5 | One generated voice fails; translation or ASR fails in separate cases. | Successful work remains saved; retry only the failed work. No false READY status. |
| A08 | M4, M5 | Inference is busy while the other user opens settings or a saved entry. | Interaction handling remains responsive and is not awaiting inference. |
| A09 | M5 | Submit 1,000/1,001 characters and voice notes of 120/121 seconds. | Boundary inputs are accepted; over-limit inputs are rejected before expensive work. |
| A10 | M6 | Alice edits German to equal another sentence in her collection. | Edit succeeds with an advisory warning. Only the edited sentence's derived data is regenerated. |
| A11 | M6 | Alice corrects English. | Only her translation changes; no TTS job is created. |
| A12 | M6 | Alice replaces original audio; generation fails midway. | Previous usable content remains available; replacement can be retried. |
| A13 | M6 | A worker finishes an old revision after a newer edit or deletion. | Its output cannot overwrite the newer revision or recreate the deleted sentence. |
| A14 | M6 | Delete one audio; regenerate it; confirm whole-sentence deletion. | Audio deletion leaves the sentence; regeneration safely replaces media; whole deletion removes dependent personal records without breaking other copies. |
| A15 | M7 | New users submit sentences; Alice later switches public. | Initially private. Only Alice's public entries are discoverable through search, random, and latest. |
| A16 | M7 | Bob opens Alice's public entry and plays its original recording. | Text and audio are available without forwarding attribution or archive-channel identifiers. |
| A17 | M7 | Bob copies Alice's sentence twice intentionally. | Two Bob-owned copies with text and generated audio, no original recording, and independent review state. A detected duplicate is advisory. |
| A18 | M7 | Bob edits a copy; Alice edits the source. | Neither overwrites the other. Bob's copy retains its immediate-source reference. |
| A19 | M7 | Alice makes her collection private after Bob saved a copy. | Source disappears from discovery and old source callbacks stop working. Bob's existing copy remains usable under Bob's settings. |
| A20 | M7 | Alice deletes the source. | Bob's copies and shared generated media survive; their source foreign keys become null. |
| A21 | M7 | A copy is itself copied. | The new copy references the immediate source, not a guessed root ancestor. Provenance never bypasses privacy. |
| A22 | M8 | Start either translation direction. | Prompt and spoiler-hidden translation arrive together with Again/Hard/Good/Easy buttons. No typed answer or reveal callback is required. |
| A23 | M8 | Review text contains `<`, `>`, and `&`. | Text is escaped correctly; only the answer is spoiler-hidden. |
| A24 | M8 | Submit a grade twice or press an old question's button. | A review is applied once, to the correct user, sentence, mode, and question. |
| A25 | M8 | Review one of two identical sentences in one mode. | Only that sentence/mode's FSRS card changes. |
| A26 | M8 | Edit German, English, or audio on a reviewed sentence. | German resets all four cards; English resets both translation modes and listening meaning; audio alone resets none. History retains revision context. |
| A27 | M8 | Required listening audio is missing. | Affected listening mode is unavailable until repaired; usable modes continue. |
| A28 | M9 | Opt in to reminders, cross a timezone/DST boundary, then restart services. | At most one reminder per local date when due cards exist; no backlog replay. |
| A29 | M4, M10 | Kill a worker during inference or archiving, then restart. | Durable work recovers; temporary files are cleaned; uncertain uploads are logged without duplicate active assets. |
| A30 | M10 | Restore a database backup using the same bot and archive. | Settings, sentences, jobs, reviews, and archived playback references remain usable. |

## M0 review

- Ownership: one user-owned `Sentence`; no global or per-user German uniqueness constraint.
- Copies are independent, with shared immutable media references and nullable immediate-source provenance.
- Privacy checks cover discovery, playback, old callbacks, duplicate warnings, and provenance.
- Duplicate content is allowed; duplicate delivery of an operation is idempotent.
- Translation recall uses spoiler flashcards; typed translation modes and source-change notifications remain future work.

M0 is complete when these scenarios and the implementation plan agree. Later milestones turn applicable scenarios into executable checks and record actual results.

## Admin approval and external deployment revision (M2)

- First /start creates one private, pending record and one channel request; repeated and concurrent starts refresh profile without duplicate requests.
- Only ADMIN_TELEGRAM_ID can open management actions or approve/reject through private or configured-channel callbacks. Forged and stale callbacks cannot alter access.
- Grant by ID works before registration. Rejected/revoked users stay blocked after /start; restore works without deleting their data. The configured admin cannot be revoked.
- Lists paginate; totals include preapproved records. Access transitions are audited. User notifications and channel edits fail independently of the committed decision.
- Native AsyncTeleBot handler failure rolls back domain changes before direct replies. Capture redelivery cannot create duplicate operations; raw Telegram updates and generic deliveries are not stored.
- Fresh Compose startup applies migrations and becomes ready on host loopback port 8060 only; database has no host port.
- External configuration, users, models, and benchmark evidence survive container recreation from a different checkout. Source artifact migration verifies checksums and retains originals.

## Durable operation and private model-service refactor

- A voice submission immediately receives a Transcribing status reply. Only identifiers, Telegram file reference, current step, results, and recovery state enter PostgreSQL.
- Transcription and translation update the same message, ending with German and English only. Other commands remain responsive during inference.
- Restarting the API resumes pending steps; restarting a model service recovers its expired lease. Late results from old claim tokens cannot overwrite current results.
- Model APIs accept stable job IDs, return existing results for identical retries, reject conflicting inputs, and apply bounded admission using PostgreSQL.
- Model ports are not published; services join only the internal Docker network. TTS produces a WAV through its internal API but is not called by the bot yet.
- Upgrading removes the raw inbox/outbox while preserving user approvals, access audit records, and known approval-message IDs.
