"""Opt-in owner-requested speech generation and durable archive upload."""

import asyncio
import logging
import re
from contextlib import suppress
from datetime import timedelta
from html import escape
from uuid import uuid4

import httpx
from sqlalchemy import and_, func, or_, select, update

from app.bot.transport import RejectedDelivery, RetryAfter
from app.db.models import AudioAsset, AudioJob, Sentence, StoredMedia, User, Voice
from app.services.archive import copy_payload

logger = logging.getLogger(__name__)
DEFAULT_VOICE = "de_DE-thorsten-medium"


def clip_telegram_text(text, limit):
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def archive_audio_metadata(sentence, asset, voice):
    voice_name = voice.id.split("-", 1)[-1].replace("-", " ").replace("_", " ").title()

    def tag(value):
        return "#" + re.sub(r"\W+", "_", str(value))

    fields = [
        ("Voice", f"{voice_name} {tag(voice.id)}"),
        ("Provider", tag(voice.provider)),
        ("Sentence", tag(f"sentence_{sentence.id.hex}")),
        ("User", tag(f"user_{sentence.user_id}")),
        ("Audio", tag(f"audio_{asset.id.hex}")),
        ("Revision", tag(f"revision_{sentence.revision}")),
    ]
    footer_plain = "\n\n" + "\n".join(f"{label}: {value}" for label, value in fields)
    overhead = "German\n\n\nEnglish\n" + footer_plain
    remaining = 1024 - len(overhead.encode("utf-16-le")) // 2
    german_length = len(sentence.german.encode("utf-16-le")) // 2
    english_length = len(sentence.english.encode("utf-16-le")) // 2
    german_limit = min(german_length, remaining // 2)
    english_limit = min(english_length, remaining - german_limit)
    german_limit = min(german_length, remaining - english_limit)
    footer = "\n\n" + "\n".join(f"<b>{label}:</b> {escape(value)}" for label, value in fields)
    title = " ".join(sentence.german.split())
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")
    filename = filename.encode("utf-8")[:200].decode("utf-8", errors="ignore") or "German sentence"
    return {
        "caption": (
            f"<b>German</b>\n{escape(clip_telegram_text(sentence.german, german_limit))}"
            f"\n\n<b>English</b>\n{escape(clip_telegram_text(sentence.english, english_limit))}"
            + footer
        ),
        "parse_mode": "HTML",
        "title": title,
        "performer": clip_telegram_text(voice_name, 64),
        "file_name": f"{filename}.mp3",
    }


async def generated_audio(session, user_id, sentence_id, voice_id=DEFAULT_VOICE):
    return await session.scalar(
        select(AudioAsset)
        .join(Sentence)
        .where(
            Sentence.user_id == user_id,
            Sentence.id == sentence_id,
            AudioAsset.kind == "generated",
            AudioAsset.voice_id == voice_id,
            AudioAsset.active,
        )
    )


async def staged_generated_audio(session, sentence_id, voice_id):
    return await session.scalar(
        select(AudioAsset).where(
            AudioAsset.sentence_id == sentence_id,
            AudioAsset.kind == "generated",
            AudioAsset.voice_id == voice_id,
            AudioAsset.active.is_(False),
            AudioAsset.replacement.is_(True),
        )
    )


async def request_audio(
    session, settings, user_id, sentence_id, voice_id=DEFAULT_VOICE, *, regenerate=False
):
    if not settings.telegram_tts_enabled:
        return None
    sentence = await session.scalar(
        select(Sentence)
        .where(
            Sentence.id == sentence_id,
            Sentence.user_id == user_id,
        )
        .with_for_update()
    )
    if sentence is None:
        return None
    voice_id = voice_id or DEFAULT_VOICE
    if await session.get(Voice, voice_id) is None:
        return None
    asset = await generated_audio(session, user_id, sentence.id, voice_id)
    job = await session.get(AudioJob, asset.id) if asset else None
    if job and job.sentence_revision != sentence.revision:
        asset.active = False
        await session.flush()
        asset, job = None, None
    if regenerate and asset and asset.state == "ready":
        staged = await staged_generated_audio(session, sentence.id, voice_id)
        staged_job = await session.get(AudioJob, staged.id) if staged else None
        if staged and staged_job and staged_job.state in {"pending", "running"}:
            return staged
        if staged and staged_job and staged_job.state == "failed":
            staged_job.generation += 1
            staged_job.state, staged_job.attempts, staged_job.error_type = "pending", 0, None
            staged_job.claim_token, staged_job.lease_until = None, None
            staged_job.available_at = func.now()
            staged.state = "generating"
            return staged
        staged = AudioAsset(
            id=uuid4(), sentence_id=sentence.id, voice_id=voice_id, kind="generated",
            state="generating", active=False, replacement=True,
        )
        session.add(staged)
        await session.flush()
        session.add(AudioJob(asset_id=staged.id, sentence_revision=sentence.revision))
        return staged
    if asset is None:
        asset = AudioAsset(
            id=uuid4(),
            sentence_id=sentence.id,
            voice_id=voice_id,
            kind="generated",
            state="generating",
        )
        session.add(asset)
        await session.flush()
    if asset.state == "ready" and asset.stored_media_id:
        return asset
    if job is None:
        session.add(AudioJob(asset_id=asset.id, sentence_revision=sentence.revision))
    elif job.state in {"failed", "cancelled"}:
        if job.step == "synthesize":
            job.generation += 1
        job.state, job.attempts, job.error_type = "pending", 0, None
        job.claim_token, job.lease_until, job.available_at = None, None, func.now()
        asset.state = "generating" if job.step == "synthesize" else "saving"
    return asset


async def playback_payload(session, user_id, asset_id):
    media = await session.scalar(
        select(StoredMedia)
        .join(AudioAsset, AudioAsset.stored_media_id == StoredMedia.id)
        .join(Sentence, Sentence.id == AudioAsset.sentence_id)
        .where(
            AudioAsset.id == asset_id,
            AudioAsset.active,
            AudioAsset.state == "ready",
            Sentence.user_id == user_id,
            StoredMedia.archive_message_id.is_not(None),
            StoredMedia.delete_pending.is_(False),
            StoredMedia.archive_chat_id.is_not(None),
        )
    )
    if media is None:
        return None
    return copy_payload(user_id, media.archive_chat_id, media.archive_message_id)


class AudioRunner:
    def __init__(self, database, settings, transport, models):
        self.database, self.settings = database, settings
        self.transport, self.models = transport, models

    async def claim(self):
        if not self.settings.telegram_tts_enabled:
            return None
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(AudioJob)
                .where(
                    AudioJob.available_at <= func.now(),
                    or_(
                        AudioJob.state == "pending",
                        and_(AudioJob.state == "running", AudioJob.lease_until < func.now()),
                    ),
                )
                .order_by(AudioJob.available_at, AudioJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            if job.state == "running" and job.step == "upload":
                job.uncertain = True
                logger.warning("tts_archive_result_uncertain", extra={"job_id": str(job.asset_id)})
            job.state, job.claim_token = "running", uuid4()
            job.attempts += 1
            job.target_chat_id = job.target_chat_id or self.settings.telegram_media_channel_id
            job.lease_until = func.now() + timedelta(seconds=60)
            await session.flush()
            return job

    async def finish(
        self,
        claimed,
        *,
        state,
        receipt=None,
        error_type=None,
        uncertain=False,
        delay=0,
        step=None,
        regenerate=False,
    ):
        async with self.database.sessions.begin() as session:
            sentence = await session.scalar(
                select(Sentence)
                .join(AudioAsset)
                .where(AudioAsset.id == claimed.asset_id)
                .with_for_update(of=Sentence)
            )
            job = await session.scalar(
                select(AudioJob)
                .where(
                    AudioJob.asset_id == claimed.asset_id,
                    AudioJob.claim_token == claimed.claim_token,
                    AudioJob.state == "running",
                )
                .with_for_update()
            )
            if job is None:
                return False
            asset = await session.get(AudioAsset, job.asset_id)
            job.claim_token, job.lease_until = None, None
            if sentence is None or not (asset.active or asset.replacement) or sentence.revision != job.sentence_revision:
                job.state, job.error_type = "cancelled", "ObsoleteRevision"
                asset.active, asset.replacement = False, False
                return False
            job.state, job.error_type = state, error_type
            job.uncertain = job.uncertain or uncertain
            job.available_at = func.now() + timedelta(seconds=delay)
            if step:
                job.step, job.attempts = step, 0
            if regenerate:
                job.generation += 1
            if receipt:
                media = StoredMedia(
                    id=uuid4(),
                    telegram_file_id=receipt.file_id,
                    archive_chat_id=job.target_chat_id,
                    archive_message_id=receipt.message_id,
                )
                session.add(media)
                await session.flush()
                asset.stored_media_id, asset.state = media.id, "ready"
                if asset.replacement and asset.kind == "generated":
                    previous = (await session.scalars(
                        select(AudioAsset).where(
                            AudioAsset.sentence_id == sentence.id,
                            AudioAsset.kind == "generated",
                            AudioAsset.voice_id == asset.voice_id,
                            AudioAsset.active.is_(True),
                            AudioAsset.id != asset.id,
                        ).with_for_update()
                    )).all()
                    for old_asset in previous:
                        old_media_id = old_asset.stored_media_id
                        old_asset.active = False
                        if old_media_id:
                            from app.services.deletion import _schedule_media_cleanup

                            await _schedule_media_cleanup(session, old_media_id)
                    asset.active, asset.replacement = True, False
            else:
                asset.state = (
                    "failed"
                    if state in {"failed", "cancelled"}
                    else ("saving" if job.step == "upload" else "generating")
                )
            return True

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            async with self.database.sessions.begin() as session:
                result = await session.execute(
                    update(AudioJob)
                    .where(
                        AudioJob.asset_id == job.asset_id,
                        AudioJob.claim_token == job.claim_token,
                        AudioJob.state == "running",
                    )
                    .values(lease_until=func.now() + timedelta(seconds=60))
                )
                if not result.rowcount:
                    return

    async def refresh_controls(self, job):
        async with self.database.sessions() as session:
            asset = await session.get(AudioAsset, job.asset_id)
            if asset is None or not asset.active or asset.state != "ready":
                return
            sentence = await session.get(Sentence, asset.sentence_id)
            user = await session.get(User, sentence.user_id)
            if (
                sentence.revision != job.sentence_revision
                or sentence.status_message_id is None
                or (
                    sentence.user_id != self.settings.admin_telegram_id
                    and user.status != "approved"
                )
            ):
                return
            payload = {
                "chat_id": sentence.user_id,
                "message_id": sentence.status_message_id,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Play German audio", "callback_data": f"play:{asset.id}"}],
                        [
                            {"text": "Open saved entry", "callback_data": f"entry:{sentence.id}"},
                            {"text": "My collection", "callback_data": "library:0"},
                        ],
                    ]
                },
            }
        async with asyncio.timeout(self.settings.telegram_timeout_seconds):
            await self.transport.send("edit_message_reply_markup", payload)

    def log_orphan(self, job, message_id):
        logger.warning(
            "tts_archive_orphan_possible",
            extra={
                "job_id": str(job.asset_id),
                "archive_chat_id": job.target_chat_id,
                "archive_message_id": message_id,
            },
        )

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(job))
        upload_started, receipt = False, None
        try:
            async with self.database.sessions() as session:
                asset = await session.get(AudioAsset, job.asset_id)
                sentence = await session.get(Sentence, asset.sentence_id)
                user = await session.get(User, sentence.user_id)
                voice = await session.get(Voice, asset.voice_id)
            if not (asset.active or asset.replacement) or sentence.revision != job.sentence_revision:
                await self.finish(job, state="cancelled", error_type="ObsoleteRevision")
                return True
            if sentence.user_id != self.settings.admin_telegram_id and user.status != "approved":
                await self.finish(job, state="cancelled", error_type="AccessRevoked")
                return True
            if job.attempts > 3:
                raise ValueError("AudioRetryLimit")
            async with asyncio.timeout(self.settings.operation_timeout_seconds):
                if job.step == "synthesize":
                    await self.models.synthesize(job, sentence.german)
                    await self.finish(job, state="pending", step="upload")
                else:
                    audio = await self.models.speech_audio(job)
                    async with asyncio.timeout(self.settings.telegram_timeout_seconds):
                        upload_started = True
                        receipt = await self.transport.upload_audio(
                            job.target_chat_id,
                            audio,
                            **archive_audio_metadata(sentence, asset, voice),
                        )
                    if await self.finish(job, state="done", receipt=receipt):
                        with suppress(Exception):
                            await self.models.discard_speech(job)
                        with suppress(Exception):
                            await self.refresh_controls(job)
                    else:
                        self.log_orphan(job, receipt.message_id)
        except httpx.HTTPStatusError as exc:
            if job.step == "upload" and exc.response.status_code == 404:
                await self.finish(
                    job,
                    state="pending",
                    step="synthesize",
                    regenerate=True,
                    error_type="ExpiredSpeechAudio",
                )
            else:
                await self.fail(job, exc, upload_started, receipt)
        except Exception as exc:
            await self.fail(job, exc, upload_started, receipt)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def fail(self, job, exc, upload_started, receipt):
        uncertain = upload_started and not isinstance(
            exc, (RejectedDelivery, RetryAfter, ValueError)
        )
        if uncertain:
            logger.warning("tts_archive_result_uncertain", extra={"job_id": str(job.asset_id)})
        message_id = receipt.message_id if receipt else getattr(exc, "message_id", None)
        if message_id is not None:
            self.log_orphan(job, message_id)
        permanent = isinstance(exc, (RejectedDelivery, ValueError)) or job.attempts >= 3
        await self.finish(
            job,
            state="failed" if permanent else "pending",
            uncertain=uncertain,
            error_type=type(exc).__name__[:100],
            delay=getattr(exc, "seconds", 2**job.attempts),
        )
        logger.warning(
            "audio_job_failed",
            extra={"job_id": str(job.asset_id), "error_type": type(exc).__name__},
        )

    async def serve(self):
        while True:
            try:
                worked = await self.process_one()
            except Exception as exc:
                logger.error("audio_worker_error", extra={"error_type": type(exc).__name__})
                worked = False
            if not worked:
                await asyncio.sleep(self.settings.worker_poll_seconds)
