"""Owner-scoped deletion and reference-aware cleanup of archived Telegram media."""

import asyncio
import logging
from datetime import timedelta

from app.core.idgen import generate_claim_token
from sqlalchemy import and_, func, or_, select

from app.bot.transport import RejectedDelivery
from app.db.models import (
    ArchiveJob,
    AudioAsset,
    AudioJob,
    MediaDeletionJob,
    Sentence,
    SentenceEditJob,
    StoredMedia,
)

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60


async def _schedule_media_cleanup(session, media_id, *, exclude_sentence_id=None):
    media = await session.scalar(
        select(StoredMedia).where(StoredMedia.id == media_id).with_for_update()
    )
    if media is None:
        return False
    active = select(func.count()).select_from(AudioAsset).where(
        AudioAsset.stored_media_id == media_id,
        AudioAsset.active.is_(True),
    )
    if exclude_sentence_id is not None:
        active = active.where(AudioAsset.sentence_id != exclude_sentence_id)
    if await session.scalar(active):
        return False

    archive_job = await session.get(ArchiveJob, media_id, with_for_update=True)
    if archive_job and archive_job.state in {"pending", "running", "failed"}:
        if archive_job.state == "pending":
            await session.delete(archive_job)
        else:
            archive_job.state = "failed"
            archive_job.error_type = "media_deleted"
            archive_job.claim_token, archive_job.lease_until = None, None

    media.delete_pending = True
    job = await session.get(MediaDeletionJob, media_id, with_for_update=True)
    if job is None:
        session.add(MediaDeletionJob(
            media_id=media.id,
            target_chat_id=media.archive_chat_id,
            target_message_id=media.archive_message_id,
        ))
    elif job.state in {"failed", "cancelled"}:
        job.target_chat_id, job.target_message_id = media.archive_chat_id, media.archive_message_id
        job.state, job.attempts, job.error_type = "pending", 0, None
        job.available_at, job.claim_token, job.lease_until = func.now(), None, None
    return True


async def delete_audio(session, user_id, asset_id):
    asset = await session.scalar(
        select(AudioAsset)
        .join(Sentence, Sentence.id == AudioAsset.sentence_id)
        .where(
            AudioAsset.id == asset_id,
            Sentence.user_id == user_id,
            or_(AudioAsset.active.is_(True), AudioAsset.replacement.is_(True)),
        )
        .with_for_update(of=AudioAsset)
    )
    if asset is None:
        return False
    asset.active, asset.replacement = False, False
    if asset.kind == "generated":
        job = await session.get(AudioJob, asset.id, with_for_update=True)
        if job and job.state in {"pending", "running"}:
            job.state, job.error_type = "cancelled", "audio_deleted"
            job.claim_token, job.lease_until = None, None
    if asset.stored_media_id:
        await _schedule_media_cleanup(session, asset.stored_media_id)
    return True


async def delete_sentence(session, user_id, sentence_id):
    sentence = await session.scalar(
        select(Sentence)
        .where(Sentence.id == sentence_id, Sentence.user_id == user_id)
        .with_for_update()
    )
    if sentence is None:
        return False
    assets = (await session.scalars(
        select(AudioAsset).where(AudioAsset.sentence_id == sentence.id).with_for_update()
    )).all()
    media_ids = {asset.stored_media_id for asset in assets if asset.stored_media_id}
    for asset in assets:
        asset.active, asset.replacement = False, False
        if asset.kind == "generated":
            job = await session.get(AudioJob, asset.id, with_for_update=True)
            if job and job.state in {"pending", "running"}:
                job.state, job.error_type = "cancelled", "entry_deleted"
                job.claim_token, job.lease_until = None, None
    edit_job = await session.get(SentenceEditJob, sentence.id, with_for_update=True)
    if edit_job and edit_job.state in {"pending", "running"}:
        edit_job.state, edit_job.error_type = "cancelled", "entry_deleted"
        edit_job.claim_token, edit_job.lease_until = None, None
    for media_id in media_ids:
        await _schedule_media_cleanup(session, media_id, exclude_sentence_id=sentence.id)
    await session.delete(sentence)
    return True


class MediaDeletionRunner:
    def __init__(self, database, settings, transport):
        self.database, self.settings, self.transport = database, settings, transport

    async def claim(self):
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(MediaDeletionJob)
                .where(
                    MediaDeletionJob.available_at <= func.now(),
                    or_(
                        MediaDeletionJob.state == "pending",
                        and_(MediaDeletionJob.state == "running", MediaDeletionJob.lease_until < func.now()),
                    ),
                )
                .order_by(MediaDeletionJob.available_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            media = await session.get(StoredMedia, job.media_id, with_for_update=True)
            if media is None:
                return None
            active_count = await session.scalar(select(func.count()).select_from(AudioAsset).where(
                AudioAsset.stored_media_id == media.id, AudioAsset.active.is_(True)
            ))
            if active_count:
                job.state, job.error_type = "cancelled", "media_referenced"
                job.claim_token, job.lease_until = None, None
                media.delete_pending = False
                return None
            media.delete_pending = True
            job.target_chat_id = media.archive_chat_id
            job.target_message_id = media.archive_message_id
            job.state, job.claim_token = "running", generate_claim_token()
            job.attempts += 1
            job.lease_until = func.now() + timedelta(seconds=LEASE_SECONDS)
            await session.flush()
            return job

    async def process_one(self):
        job = await self.claim()
        if job is None:
            return False
        try:
            if job.target_chat_id is not None and job.target_message_id is not None:
                if job.attempts > 3:
                    raise RejectedDelivery("Cleanup retry limit")
                async with asyncio.timeout(self.settings.telegram_timeout_seconds):
                    deleted = await self.transport.send("delete_message", {
                        "chat_id": job.target_chat_id,
                        "message_id": job.target_message_id,
                    })
                if not deleted:
                    raise RejectedDelivery("Archive message could not be deleted")
            async with self.database.sessions.begin() as session:
                current = await session.get(MediaDeletionJob, job.media_id, with_for_update=True)
                media = await session.get(StoredMedia, job.media_id, with_for_update=True)
                if current is None or current.claim_token != job.claim_token or media is None:
                    return True
                active_count = await session.scalar(select(func.count()).select_from(AudioAsset).where(
                    AudioAsset.stored_media_id == media.id, AudioAsset.active.is_(True)
                ))
                if active_count:
                    current.state, current.error_type = "cancelled", "media_referenced"
                    current.claim_token, current.lease_until = None, None
                    media.delete_pending = False
                    return True
                await session.execute(
                    AudioAsset.__table__.update()
                    .where(AudioAsset.stored_media_id == media.id)
                    .values(stored_media_id=None)
                )
                archive_job = await session.get(ArchiveJob, media.id, with_for_update=True)
                if archive_job:
                    await session.delete(archive_job)
                await session.delete(media)
            logger.info("archive_media_deleted", extra={
                "media_id": str(job.media_id),
                "archive_chat_id": job.target_chat_id,
                "archive_message_id": job.target_message_id,
            })
        except Exception as exc:
            permanent = isinstance(exc, RejectedDelivery) or job.attempts >= 3
            async with self.database.sessions.begin() as session:
                current = await session.get(MediaDeletionJob, job.media_id, with_for_update=True)
                media = await session.get(StoredMedia, job.media_id, with_for_update=True)
                if current and current.claim_token == job.claim_token:
                    current.state = "failed" if permanent else "pending"
                    current.error_type = type(exc).__name__[:100]
                    current.claim_token, current.lease_until = None, None
                    current.available_at = func.now() + timedelta(seconds=min(60, 2**job.attempts))
                    if media:
                        media.delete_pending = not permanent
            logger.warning("archive_media_delete_failed", extra={
                "media_id": str(job.media_id), "error_type": type(exc).__name__
            })
        return True

    async def serve(self):
        async def worker():
            while True:
                try:
                    worked = await self.process_one()
                except Exception as exc:
                    logger.error("media_deletion_worker_error", extra={"error_type": type(exc).__name__})
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
