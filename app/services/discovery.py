"""Current-visibility public queries, privacy changes, and independent copies."""

from uuid import uuid4

from sqlalchemy import func, or_, select

from app.db.models import AudioAsset, Sentence, StoredMedia, User
from app.services.collections import has_duplicate, normalize_german
from app.services.scheduling import ensure_review_cards


def _public_sentence_query():
    return (
        select(Sentence)
        .join(User, User.id == Sentence.user_id)
        .where(Sentence.state == "ready", User.status == "approved", User.private.is_(False))
    )


async def public_sentence(session, sentence_id):
    return await session.scalar(
        _public_sentence_query().where(Sentence.id == sentence_id)
    )


async def search_public(session, query, page=0, page_size=5):
    term = query.strip()
    if not term or len(term) > 100:
        return []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return (await session.scalars(
        _public_sentence_query()
        .where(or_(Sentence.german.ilike(pattern, escape="\\"), Sentence.english.ilike(pattern, escape="\\")))
        .order_by(Sentence.created_at.desc(), Sentence.id.desc())
        .offset(page * page_size)
        .limit(page_size + 1)
    )).all()


async def latest_public(session, page=0, page_size=5):
    return (await session.scalars(
        _public_sentence_query()
        .order_by(Sentence.created_at.desc(), Sentence.id.desc())
        .offset(page * page_size)
        .limit(page_size + 1)
    )).all()


async def random_public(session):
    return await session.scalar(_public_sentence_query().order_by(func.random()).limit(1))


async def set_privacy(session, user_id, *, private):
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.status != "approved":
        return None
    user.private = private
    return user.private


async def copy_public_sentence(session, user_id, sentence_id, settings=None):
    source_owner_id = await session.scalar(
        select(Sentence.user_id).where(Sentence.id == sentence_id)
    )
    if source_owner_id is None:
        return None
    users = (await session.scalars(
        select(User)
        .where(User.id.in_({user_id, source_owner_id}))
        .order_by(User.id)
        .with_for_update()
    )).all()
    by_id = {user.id: user for user in users}
    owner, source_owner = by_id.get(user_id), by_id.get(source_owner_id)
    if (
        owner is None
        or owner.status != "approved"
        or source_owner is None
        or source_owner.status != "approved"
        or source_owner.private
    ):
        return None
    source = await session.scalar(
        select(Sentence).where(
            Sentence.id == sentence_id,
            Sentence.user_id == source_owner_id,
            Sentence.state == "ready",
        ).with_for_update()
    )
    if source is None:
        return None
    sentence = Sentence(
        id=uuid4(),
        user_id=user_id,
        german=source.german,
        normalized_german=normalize_german(source.german),
        english=source.english,
        duplicate_warning=await has_duplicate(session, user_id, source.german),
        copied_from_sentence_id=source.id,
    )
    session.add(sentence)
    await session.flush()
    await ensure_review_cards(session, sentence)
    assets = (await session.scalars(
        select(AudioAsset)
        .join(StoredMedia, StoredMedia.id == AudioAsset.stored_media_id)
        .where(
            AudioAsset.sentence_id == source.id,
            AudioAsset.kind == "generated",
            AudioAsset.active.is_(True),
            AudioAsset.state == "ready",
            StoredMedia.archive_chat_id.is_not(None),
            StoredMedia.archive_message_id.is_not(None),
            StoredMedia.delete_pending.is_(False),
        )
    )).all()
    for asset in assets:
        media = await session.scalar(
            select(StoredMedia).where(StoredMedia.id == asset.stored_media_id).with_for_update()
        )
        if media is None or media.delete_pending:
            continue
        session.add(AudioAsset(
            id=uuid4(), sentence_id=sentence.id, stored_media_id=media.id,
            voice_id=asset.voice_id, kind="generated", state="ready", active=True,
        ))
    if settings is not None:
        from app.services.audio import enqueue_missing_voice_audio

        await enqueue_missing_voice_audio(session, settings, sentence)
    return sentence
