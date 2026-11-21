"""Replace legacy UUID captions on generated archive messages with typed custom IDs."""

import asyncio
import os

from sqlalchemy import select

from app.bot.transport import BotTransport, RetryAfter
from app.config import Settings
from app.db.models import AudioAsset, Sentence, StoredMedia, User, Voice
from app.db.session import Database
from app.services.audio import archive_audio_metadata


async def refresh():
    settings = Settings()
    database = Database(settings.database_url.get_secret_value())
    transport = BotTransport(settings.telegram_bot_token.get_secret_value())
    updated = skipped = 0
    try:
        async with database.sessions() as session:
            rows = (
                await session.execute(
                    select(Sentence, AudioAsset, StoredMedia, Voice, User)
                    .join(AudioAsset, AudioAsset.sentence_id == Sentence.id)
                    .join(StoredMedia, StoredMedia.id == AudioAsset.stored_media_id)
                    .join(Voice, Voice.id == AudioAsset.voice_id)
                    .join(User, User.id == Sentence.user_id)
                    .where(
                        AudioAsset.kind == "generated",
                        AudioAsset.active.is_(True),
                        StoredMedia.archive_chat_id.is_not(None),
                        StoredMedia.archive_message_id.is_not(None),
                    )
                    .order_by(Sentence.created_at, AudioAsset.id)
                    .limit(int(os.environ.get("ARCHIVE_METADATA_REFRESH_LIMIT", "1000000")))
                )
            ).all()

        semaphore = asyncio.Semaphore(3)

        async def refresh_one(sentence, asset, media, voice, user):
            async with semaphore:
                payload = archive_audio_metadata(sentence, asset, voice, user)
                try:
                    while True:
                        try:
                            async with asyncio.timeout(settings.telegram_timeout_seconds):
                                await transport.send(
                                    "edit_message_caption",
                                    {
                                        "chat_id": media.archive_chat_id,
                                        "message_id": media.archive_message_id,
                                        "caption": payload["caption"],
                                        "parse_mode": payload["parse_mode"],
                                    },
                                )
                            return True
                        except RetryAfter as exc:
                            await asyncio.sleep(exc.seconds)
                except Exception as exc:
                    print(
                        f"Could not update #AU_{asset.id} in archive "
                        f"{media.archive_message_id}: {type(exc).__name__}",
                        flush=True,
                    )
                    return False

        results = await asyncio.gather(*(refresh_one(*row) for row in rows))
        updated = sum(results)
        skipped = len(results) - updated
        print(f"Processed archive captions: {len(results)}", flush=True)
        print(f"Updated archive captions: {updated}; skipped: {skipped}")
        if skipped:
            raise RuntimeError("Some archive captions could not be refreshed")
    finally:
        await transport.close()
        await database.close()


def run():
    asyncio.run(refresh())


if __name__ == "__main__":
    run()
