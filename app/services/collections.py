"""Personal collections and advisory duplicate checks, scoped at the query boundary."""

import unicodedata
from uuid import uuid4

from sqlalchemy import or_, select

from app.db.models import ArchiveJob, AudioAsset, Operation, Sentence, StoredMedia, User
from app.services.scheduling import ensure_review_cards

DUPLICATE_WARNING = "Similar German text already exists in your collection or a public collection."


def normalize_german(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


async def has_duplicate(session, user_id, german, exclude_id=None):
    query = (
        select(Sentence.id)
        .join(User, User.id == Sentence.user_id)
        .where(
            Sentence.normalized_german == normalize_german(german),
            or_(Sentence.user_id == user_id, User.private.is_(False)),
        )
        .limit(1)
    )
    if exclude_id is not None:
        query = query.where(Sentence.id != exclude_id)
    return await session.scalar(query) is not None


async def get_owned_sentence(session, user_id, sentence_id):
    return await session.scalar(
        select(Sentence).where(Sentence.id == sentence_id, Sentence.user_id == user_id)
    )


async def list_sentences(session, user_id, page=0, page_size=5):
    return (
        await session.scalars(
            select(Sentence)
            .where(Sentence.user_id == user_id)
            .order_by(Sentence.created_at.desc(), Sentence.id.desc())
            .offset(page * page_size)
            .limit(page_size + 1)
        )
    ).all()


async def save_capture(session, claimed, settings=None):
    # Serialize against lease recovery and completion retries before creating any entry.
    operation = await session.scalar(
        select(Operation)
        .where(
            Operation.id == claimed.id,
            Operation.claim_token == claimed.claim_token,
            Operation.state == "running",
            Operation.step == "complete",
        )
        .with_for_update()
    )
    if operation is None:
        return None
    sentence = await session.scalar(
        select(Sentence).where(Sentence.capture_operation_id == operation.id)
    )
    if sentence is not None:
        sentence.status_message_id = operation.status_message_id
        if settings is not None:
            from app.services.audio import enqueue_missing_voice_audio

            await enqueue_missing_voice_audio(session, settings, sentence)
        return sentence
    sentence = Sentence(
        id=uuid4(),
        user_id=operation.user_id,
        capture_operation_id=operation.id,
        german=operation.german,
        english=operation.english,
        normalized_german=normalize_german(operation.german),
        duplicate_warning=await has_duplicate(session, operation.user_id, operation.german),
        status_message_id=operation.status_message_id,
    )
    session.add(sentence)
    await session.flush()
    await ensure_review_cards(session, sentence)
    if operation.source_file_id:
        media = StoredMedia(id=uuid4(), telegram_file_id=operation.source_file_id)
        session.add(media)
        await session.flush()
        session.add(
            AudioAsset(
                id=uuid4(),
                sentence_id=sentence.id,
                stored_media_id=media.id,
                kind="original",
                state="archiving",
            )
        )
        session.add(
            ArchiveJob(
                media_id=media.id,
                source_chat_id=operation.chat_id,
                source_message_id=operation.source_message_id,
            )
        )
    if settings is not None:
        from app.services.audio import enqueue_missing_voice_audio

        await enqueue_missing_voice_audio(session, settings, sentence)
    return sentence
