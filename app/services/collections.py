"""Personal collections and advisory duplicate checks, scoped at the query boundary."""

import base64
import binascii
import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from app.core.idgen import generate_id, normalize_id
from sqlalchemy import and_, or_, select

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


def _library_cursor_fingerprint(language, term):
    return hashlib.blake2s(f"{language}\0{term.casefold()}".encode(), digest_size=6).hexdigest()


def _encode_library_cursor(sentence, fingerprint):
    created = sentence.created_at.astimezone(timezone.utc)
    delta = created - datetime(1970, 1, 1, tzinfo=timezone.utc)
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    value = f"{fingerprint}:{micros}:{sentence.id}".encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_library_cursor(value, fingerprint):
    if not value or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        stored_fingerprint, raw_micros, sentence_id = raw.split(":", 2)
        if stored_fingerprint != fingerprint:
            return None
        sentence_id = normalize_id(sentence_id)
        micros = int(raw_micros)
        if micros < 0:
            return None
        created = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)
        return created, sentence_id
    except (ValueError, OverflowError, UnicodeDecodeError, binascii.Error):
        return None


async def search_library(session, user_id, language, query, *, offset="", limit=20):
    """Search one owner's library newest-first using a stable, bounded keyset cursor."""
    column = {"de": Sentence.german, "en": Sentence.english}.get(language)
    term = " ".join(query.split())[:100]
    if column is None:
        return [], None
    limit = min(max(int(limit), 1), 20)
    fingerprint = _library_cursor_fingerprint(language, term)
    cursor = _decode_library_cursor(offset, fingerprint)
    query_stmt = select(Sentence).where(Sentence.user_id == user_id)
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query_stmt = query_stmt.where(column.ilike(f"%{escaped}%", escape="\\"))
    if cursor:
        created, sentence_id = cursor
        query_stmt = query_stmt.where(
            or_(
                Sentence.created_at < created,
                and_(Sentence.created_at == created, Sentence.id < sentence_id),
            )
        )
    rows = (
        await session.scalars(
            query_stmt.order_by(Sentence.created_at.desc(), Sentence.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_offset = _encode_library_cursor(rows[-1], fingerprint) if has_more and rows else None
    return rows, next_offset


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
        id=generate_id(),
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
        media = StoredMedia(id=generate_id(), telegram_file_id=operation.source_file_id)
        session.add(media)
        await session.flush()
        session.add(
            AudioAsset(
                id=generate_id(),
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
