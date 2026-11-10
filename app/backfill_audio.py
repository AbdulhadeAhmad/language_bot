"""Queue missing generated German audio for saved, approved-user entries."""

import asyncio

from sqlalchemy import or_, select

from app.config import Settings
from app.db.models import AudioJob, Sentence, User, Voice
from app.db.session import Database
from app.services.audio import generated_audio, request_audio


async def backfill():
    settings = Settings()
    if not settings.telegram_tts_enabled:
        raise RuntimeError("TELEGRAM_TTS_ENABLED must be true to backfill generated audio")
    database = Database(settings.database_url.get_secret_value())
    queued = ready = skipped_uncertain = 0
    try:
        async with database.sessions.begin() as session:
            voices = (await session.scalars(select(Voice).order_by(Voice.id))).all()
            sentences = (
                await session.scalars(
                    select(Sentence)
                    .join(User, User.id == Sentence.user_id)
                    .where(
                        Sentence.state == "ready",
                        or_(User.status == "approved", User.id == settings.admin_telegram_id),
                    )
                    .order_by(Sentence.created_at, Sentence.id)
                )
            ).all()
            for sentence in sentences:
                for voice in voices:
                    asset = await generated_audio(session, sentence.user_id, sentence.id, voice.id)
                    job = await session.get(AudioJob, asset.id) if asset else None
                    if (
                        asset
                        and asset.state == "ready"
                        and job
                        and job.sentence_revision == sentence.revision
                    ):
                        ready += 1
                        continue
                    if job and job.state == "failed" and job.step == "upload" and job.uncertain:
                        skipped_uncertain += 1
                        continue
                    asset = await request_audio(
                        session, settings, sentence.user_id, sentence.id, voice.id
                    )
                    if asset is None:
                        continue
                    if asset.state == "ready":
                        ready += 1
                    else:
                        queued += 1
        print(
            f"Eligible entries: {len(sentences)}; ready voice assets: {ready}; "
            f"queued or resumed: {queued}; skipped ambiguous uploads: {skipped_uncertain}"
        )
    finally:
        await database.close()


def run():
    asyncio.run(backfill())
