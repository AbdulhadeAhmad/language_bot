"""Opt-in leased archive work; no network calls hold database transactions open."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from app.core.idgen import generate_claim_token
from sqlalchemy import and_, func, or_, select, update

from app.bot.transport import RejectedDelivery, RetryAfter
from app.db.models import ArchiveJob, AudioAsset, Sentence, StoredMedia

logger = logging.getLogger(__name__)


def copy_payload(target, source_chat, source_message):
    # Clear captions/keyboards as well as avoiding forwarding attribution.
    return {
        "chat_id": target,
        "from_chat_id": source_chat,
        "message_id": source_message,
        "caption": "",
        "reply_markup": {"inline_keyboard": []},
        "disable_notification": True,
    }


async def original_recording(session, user_id, sentence_id):
    return (
        await session.execute(
            select(AudioAsset, StoredMedia)
            .join(StoredMedia, AudioAsset.stored_media_id == StoredMedia.id)
            .join(Sentence, AudioAsset.sentence_id == Sentence.id)
            .where(
                Sentence.id == sentence_id,
                Sentence.user_id == user_id,
                AudioAsset.kind == "original",
                AudioAsset.active,
            )
        )
    ).first()


async def retry_archive(session, user_id, sentence_id):
    job = await session.scalar(
        select(ArchiveJob)
        .join(AudioAsset, AudioAsset.stored_media_id == ArchiveJob.media_id)
        .join(Sentence, Sentence.id == AudioAsset.sentence_id)
        .where(
            Sentence.id == sentence_id,
            Sentence.user_id == user_id,
            AudioAsset.kind == "original",
            (AudioAsset.active.is_(True) | AudioAsset.replacement.is_(True)),
            ArchiveJob.state == "failed",
        )
        .order_by(AudioAsset.replacement.desc())
        .with_for_update(of=(ArchiveJob, AudioAsset))
    )
    if job is None:
        return False
    job.state, job.attempts, job.error_type = "pending", 0, None
    job.claim_token, job.lease_until, job.available_at = None, None, func.now()
    await session.execute(
        update(AudioAsset)
        .where(AudioAsset.stored_media_id == job.media_id)
        .where(AudioAsset.active.is_(True) | AudioAsset.replacement.is_(True))
        .values(state="archiving")
    )
    return True


class TelegramArchiveStore:
    def __init__(self, transport):
        self.transport = transport

    async def save(self, job):
        message_id = await self.transport.send(
            "copy_message",
            copy_payload(job.target_chat_id, job.source_chat_id, job.source_message_id),
        )
        if not isinstance(message_id, int) or message_id <= 0:
            raise ConnectionError("Archive receipt unavailable")
        return message_id


class ArchiveRunner:
    def __init__(self, database, settings, transport):
        self.database, self.settings = database, settings
        self.store = TelegramArchiveStore(transport)

    async def claim(self):
        if (
            not self.settings.telegram_archive_enabled
            or self.settings.telegram_media_channel_id is None
        ):
            return None
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(ArchiveJob)
                .where(
                    ArchiveJob.available_at <= func.now(),
                    or_(
                        ArchiveJob.state == "pending",
                        and_(ArchiveJob.state == "running", ArchiveJob.lease_until < func.now()),
                    ),
                )
                .order_by(ArchiveJob.available_at, ArchiveJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            if job.state == "running":
                job.uncertain = True
                logger.warning("archive_result_uncertain", extra={"media_id": str(job.media_id)})
            job.target_chat_id = job.target_chat_id or self.settings.telegram_media_channel_id
            job.state, job.claim_token = "running", generate_claim_token()
            job.attempts += 1
            job.lease_until = func.now() + timedelta(seconds=60)
            await session.flush()
            return job

    async def finish(
        self, claimed, *, state, message_id=None, error_type=None, uncertain=False, delay=0
    ):
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(ArchiveJob)
                .where(
                    ArchiveJob.media_id == claimed.media_id,
                    ArchiveJob.claim_token == claimed.claim_token,
                    ArchiveJob.state == "running",
                )
                .with_for_update()
            )
            if job is None:
                return False
            if state == "done":
                media = await session.get(StoredMedia, job.media_id, with_for_update=True)
                if media is None or media.delete_pending:
                    job.state, job.error_type = "failed", "media_deletion_pending"
                    job.claim_token, job.lease_until = None, None
                    return False
            job.state, job.error_type = state, error_type
            job.uncertain = job.uncertain or uncertain
            job.claim_token, job.lease_until = None, None
            job.available_at = func.now() + timedelta(seconds=delay)
            if state == "done":
                media = await session.get(StoredMedia, job.media_id)
                media.archive_chat_id, media.archive_message_id = job.target_chat_id, message_id
            assets = (await session.scalars(
                select(AudioAsset).where(AudioAsset.stored_media_id == job.media_id).with_for_update()
            )).all()
            if state == "done":
                for asset in assets:
                    if asset.kind == "original" and asset.replacement:
                        await session.execute(
                            update(AudioAsset)
                            .where(
                                AudioAsset.sentence_id == asset.sentence_id,
                                AudioAsset.kind == "original",
                                AudioAsset.active.is_(True),
                            )
                            .values(active=False)
                        )
                        asset.active, asset.replacement = True, False
            for asset in assets:
                if asset.active or asset.replacement:
                    asset.state = {"done": "ready", "failed": "archive_failed"}.get(state, "archiving")
            return True

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(10)
            async with self.database.sessions.begin() as session:
                result = await session.execute(
                    update(ArchiveJob)
                    .where(
                        ArchiveJob.media_id == job.media_id,
                        ArchiveJob.claim_token == job.claim_token,
                        ArchiveJob.state == "running",
                    )
                    .values(lease_until=func.now() + timedelta(seconds=60))
                )
                if not result.rowcount:
                    return

    def log_orphan(self, job, message_id):
        logger.warning(
            "archive_orphan_possible",
            extra={
                "media_id": str(job.media_id),
                "archive_chat_id": job.target_chat_id,
                "archive_message_id": message_id,
            },
        )

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(job))
        message_id = None
        try:
            if job.attempts > 3:
                raise ValueError("ArchiveRetryLimit")
            async with asyncio.timeout(self.settings.telegram_timeout_seconds):
                message_id = await self.store.save(job)
            accepted = await self.finish(job, state="done", message_id=message_id)
            if not accepted:
                self.log_orphan(job, message_id)
        except Exception as exc:
            uncertain = not isinstance(exc, (RejectedDelivery, RetryAfter, ValueError))
            if uncertain:
                logger.warning("archive_result_uncertain", extra={"media_id": str(job.media_id)})
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
                "archive_job_failed",
                extra={
                    "media_id": str(job.media_id),
                    "error_type": type(exc).__name__,
                },
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def serve(self):
        while True:
            try:
                worked = await self.process_one()
            except Exception as exc:
                logger.error("archive_worker_error", extra={"error_type": type(exc).__name__})
                worked = False
            if not worked:
                await asyncio.sleep(self.settings.worker_poll_seconds)
