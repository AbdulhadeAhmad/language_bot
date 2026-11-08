"""Durable, revision-fenced German sentence edits and translation regeneration."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from app.db.models import AudioAsset, AudioJob, Sentence, SentenceEditJob, User
from app.services.collections import has_duplicate, normalize_german

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60


class EditRunner:
    def __init__(self, database, settings, transport, models):
        self.database, self.settings = database, settings
        self.transport, self.models = transport, models

    async def claim(self):
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(SentenceEditJob)
                .where(
                    SentenceEditJob.available_at <= func.now(),
                    or_(
                        SentenceEditJob.state == "pending",
                        and_(SentenceEditJob.state == "running", SentenceEditJob.lease_until < func.now()),
                    ),
                )
                .order_by(SentenceEditJob.available_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.state, job.claim_token = "running", uuid4()
            job.attempts += 1
            job.lease_until = func.now() + timedelta(seconds=LEASE_SECONDS)
            await session.flush()
            return job

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            async with self.database.sessions.begin() as session:
                current = await session.get(SentenceEditJob, job.sentence_id, with_for_update=True)
                if current is None or current.claim_token != job.claim_token or current.state != "running":
                    return
                current.lease_until = func.now() + timedelta(seconds=LEASE_SECONDS)

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(job))
        terminal = False
        user_id = None
        try:
            async with self.database.sessions() as session:
                sentence = await session.get(Sentence, job.sentence_id)
                user_id = sentence.user_id if sentence else None
                user = await session.get(User, user_id) if user_id else None
                allowed = user_id == self.settings.admin_telegram_id or (user and user.status == "approved")
            if not allowed:
                await self.finish_cancelled(job)
                return True
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                english = await self.models.translate(
                    SimpleNamespace(id=job.sentence_id, generation=job.generation, german=job.german)
                )
            if not english.strip() or len(english) > 20000:
                raise ValueError("TranslationLength")
            async with self.database.sessions.begin() as session:
                current = await session.get(SentenceEditJob, job.sentence_id, with_for_update=True)
                sentence = await session.get(Sentence, job.sentence_id, with_for_update=True)
                if current is None or current.claim_token != job.claim_token or current.state != "running":
                    return True
                if sentence is None or sentence.revision != current.base_revision:
                    current.state, current.claim_token, current.lease_until = "cancelled", None, None
                    return True
                sentence.german = current.german
                sentence.normalized_german = normalize_german(current.german)
                sentence.english = english.strip()
                sentence.revision = current.target_revision
                sentence.review_revision += 1
                from app.services.reviews import reset_review_schedules

                await reset_review_schedules(session, sentence, (
                    "de_to_en", "en_to_de", "dictation", "listening"
                ))
                sentence.duplicate_warning = await has_duplicate(
                    session, sentence.user_id, current.german, exclude_id=sentence.id
                )
                assets = (await session.scalars(
                    select(AudioAsset).where(
                        AudioAsset.sentence_id == sentence.id,
                        AudioAsset.kind == "generated",
                        AudioAsset.active.is_(True),
                    )
                )).all()
                voices_to_regenerate = set()
                for asset in assets:
                    asset.active = False
                    if asset.voice_id and self.settings.telegram_tts_enabled:
                        voices_to_regenerate.add(asset.voice_id)
                    if asset.stored_media_id:
                        from app.services.deletion import _schedule_media_cleanup

                        await _schedule_media_cleanup(session, asset.stored_media_id)
                    audio_job = await session.get(AudioJob, asset.id, with_for_update=True)
                    if audio_job and audio_job.state in {"pending", "running"}:
                        audio_job.state, audio_job.error_type = "cancelled", "sentence_edited"
                        audio_job.claim_token, audio_job.lease_until = None, None
                await session.flush()
                for voice_id in voices_to_regenerate:
                    replacement = AudioAsset(
                        id=uuid4(), sentence_id=sentence.id, voice_id=voice_id,
                        kind="generated", state="generating",
                    )
                    session.add(replacement)
                    await session.flush()
                    session.add(AudioJob(
                        asset_id=replacement.id, sentence_revision=sentence.revision
                    ))
                current.state, current.claim_token, current.lease_until = "done", None, None
                current.error_type = None
                user_id = sentence.user_id
            terminal = True
            with suppress(Exception):
                await self.transport.send("send_message", {
                    "chat_id": user_id,
                    "text": f"Entry updated.\n\nGerman:\n{job.german}\n\nEnglish:\n{english.strip()}\n\nOpen /library to manage its audio.",
                })
        except Exception as exc:
            permanent = isinstance(exc, ValueError) or job.attempts >= 3
            async with self.database.sessions.begin() as session:
                current = await session.get(SentenceEditJob, job.sentence_id, with_for_update=True)
                if current and current.claim_token == job.claim_token and current.state == "running":
                    current.state = "failed" if permanent else "pending"
                    current.error_type = type(exc).__name__[:100]
                    current.claim_token, current.lease_until = None, None
                    current.available_at = func.now() + timedelta(seconds=min(60, 2**job.attempts))
                    terminal = permanent
            logger.warning("sentence_edit_failed", extra={"job_id": str(job.sentence_id), "error_type": type(exc).__name__})
            if terminal and user_id:
                with suppress(Exception):
                    await self.transport.send("send_message", {
                        "chat_id": user_id,
                        "text": "Could not translate the edit. Your saved entry is unchanged. Retry by editing it again.",
                    })
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def finish_cancelled(self, job):
        async with self.database.sessions.begin() as session:
            current = await session.get(SentenceEditJob, job.sentence_id, with_for_update=True)
            if current and current.claim_token == job.claim_token:
                current.state, current.claim_token, current.lease_until = "cancelled", None, None

    async def serve(self):
        async def worker():
            while True:
                try:
                    worked = await self.process_one()
                except Exception as exc:
                    logger.error("sentence_edit_worker_error", extra={"error_type": type(exc).__name__})
                    worked = False
                if not worked:
                    await asyncio.sleep(self.settings.worker_poll_seconds)

        workers = [asyncio.create_task(worker()) for _ in range(self.settings.operation_concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
