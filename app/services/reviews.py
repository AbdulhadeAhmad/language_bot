"""Persistent single-mode review sessions and fenced grading operations."""

from datetime import UTC, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from app.db.models import (
    AudioAsset,
    InteractionState,
    ReviewAttempt,
    ReviewCard,
    ReviewSession,
    Sentence,
    StoredMedia,
    User,
)
from app.services.scheduling import REVIEW_MODES, ensure_review_cards, new_card, review_card

TYPED_MODES = {"dictation", "listening"}


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=UTC)


def _card_query(review_session, user):
    query = (
        select(ReviewCard)
        .join(Sentence, Sentence.id == ReviewCard.sentence_id)
        .where(
            Sentence.user_id == user.id,
            Sentence.state == "ready",
            ReviewCard.mode == review_session.mode,
        )
    )
    if review_session.mode in TYPED_MODES:
        available_audio = (
            select(AudioAsset.id)
            .join(StoredMedia, StoredMedia.id == AudioAsset.stored_media_id)
            .where(
                AudioAsset.sentence_id == Sentence.id,
                AudioAsset.kind == "generated",
                AudioAsset.active.is_(True),
                AudioAsset.state == "ready",
                StoredMedia.archive_chat_id.is_not(None),
                StoredMedia.archive_message_id.is_not(None),
                StoredMedia.delete_pending.is_(False),
            )
            .exists()
        )
        query = query.where(available_audio)
    return query


async def _next_question(session, review_session, user, now):
    if review_session.questions_answered >= user.review_session_limit:
        review_session.state = "completed"
        review_session.completed_at = now
        review_session.current_sentence_id = None
        return None

    due = await session.scalar(
        _card_query(review_session, user)
        .where(ReviewCard.first_reviewed_at.is_not(None), ReviewCard.due_at <= now)
        .order_by(ReviewCard.due_at, Sentence.created_at, Sentence.id)
        .limit(1)
    )
    if due is None:
        new_today = await session.scalar(
            select(func.count())
            .select_from(ReviewCard)
            .join(Sentence, Sentence.id == ReviewCard.sentence_id)
            .where(
                Sentence.user_id == user.id,
                ReviewCard.first_reviewed_at >= _day_start(now),
            )
        )
        if new_today < user.new_cards_per_day:
            due = await session.scalar(
                _card_query(review_session, user)
                .where(ReviewCard.first_reviewed_at.is_(None), ReviewCard.due_at <= now)
                .order_by(Sentence.created_at, Sentence.id)
                .limit(1)
            )
    if due is None:
        review_session.state = "completed"
        review_session.completed_at = now
        review_session.current_sentence_id = None
        return None

    sentence = await session.get(Sentence, due.sentence_id)
    review_session.question_number += 1
    review_session.current_sentence_id = sentence.id
    review_session.question_revision = sentence.review_revision
    review_session.current_audio_asset_id = None
    review_session.awaiting_answer = review_session.mode in TYPED_MODES
    review_session.submitted_answer = None
    if due.first_reviewed_at is None:
        review_session.new_cards_seen += 1
    if review_session.mode in TYPED_MODES:
        review_session.current_audio_asset_id = await session.scalar(
            select(AudioAsset.id)
            .join(StoredMedia, StoredMedia.id == AudioAsset.stored_media_id)
            .where(
                AudioAsset.sentence_id == sentence.id,
                AudioAsset.kind == "generated",
                AudioAsset.active.is_(True),
                AudioAsset.state == "ready",
                StoredMedia.archive_chat_id.is_not(None),
                StoredMedia.archive_message_id.is_not(None),
                StoredMedia.delete_pending.is_(False),
            )
            .order_by(func.random())
            .limit(1)
        )
        if review_session.current_audio_asset_id is None:
            review_session.current_sentence_id = None
            review_session.state = "completed"
            review_session.completed_at = now
            return None
    return sentence


async def start_session(session, user_id: int, mode: str, now: datetime | None = None):
    if mode not in REVIEW_MODES:
        return None, "invalid_mode"
    now = (now or datetime.now(UTC)).astimezone(UTC)
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.status != "approved":
        return None, "not_approved"
    if await session.get(InteractionState, user_id) is not None:
        return None, "pending_edit"

    active = await session.scalar(
        select(ReviewSession)
        .where(ReviewSession.user_id == user_id, ReviewSession.state == "active")
        .with_for_update()
    )
    if active:
        if active.mode == mode and active.current_sentence_id is not None:
            sentence = await session.get(Sentence, active.current_sentence_id)
            if sentence is not None:
                return (active, sentence), "resumed"
        active.state = "cancelled"
        active.completed_at = now
        active.current_sentence_id = None

    sentences = (await session.scalars(
        select(Sentence).where(Sentence.user_id == user_id, Sentence.state == "ready")
    )).all()
    for sentence in sentences:
        await ensure_review_cards(session, sentence, now)

    review_session = ReviewSession(id=uuid4(), user_id=user_id, mode=mode)
    session.add(review_session)
    await session.flush()
    sentence = await _next_question(session, review_session, user, now)
    if sentence is None:
        return None, "no_cards"
    return (review_session, sentence), "started"


async def grade_question(
    session,
    user_id: int,
    session_id: UUID,
    question_number: int,
    rating: int,
    now: datetime | None = None,
):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    observed = await session.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.id == session_id,
            ReviewSession.user_id == user_id,
            ReviewSession.state == "active",
            ReviewSession.question_number == question_number,
            ReviewSession.current_sentence_id.is_not(None),
        )
    )
    if observed is None or observed.mode not in REVIEW_MODES:
        return None, "expired"
    sentence = await session.scalar(
        select(Sentence).where(
            Sentence.id == observed.current_sentence_id,
            Sentence.user_id == user_id,
            Sentence.state == "ready",
        ).with_for_update()
    )
    review_session = await session.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.id == session_id,
            ReviewSession.user_id == user_id,
            ReviewSession.state == "active",
            ReviewSession.question_number == question_number,
            ReviewSession.current_sentence_id == observed.current_sentence_id,
        )
        .with_for_update()
    )
    if review_session is None:
        return None, "expired"
    if sentence is None or sentence.review_revision != review_session.question_revision:
        return None, "stale"
    if review_session.mode in TYPED_MODES and (
        review_session.awaiting_answer or review_session.submitted_answer is None
    ):
        return None, "answer_required"
    card = await session.scalar(
        select(ReviewCard)
        .where(
            ReviewCard.sentence_id == sentence.id,
            ReviewCard.mode == review_session.mode,
        )
        .with_for_update()
    )
    if card is None:
        return None, "stale"
    try:
        next_state, _log = review_card(card.card_state, rating, now)
    except ValueError:
        return None, "invalid_rating"

    session.add(ReviewAttempt(
        id=uuid4(),
        session_id=review_session.id,
        sentence_id=sentence.id,
        mode=review_session.mode,
        question_number=question_number,
        sentence_revision=sentence.review_revision,
        rating=rating,
        submitted_answer=review_session.submitted_answer,
    ))
    card.card_state = next_state
    card.due_at = datetime.fromisoformat(next_state["due"])
    card.last_review_at = now
    if card.first_reviewed_at is None:
        card.first_reviewed_at = now
    review_session.questions_answered += 1
    review_session.current_sentence_id = None
    review_session.current_audio_asset_id = None
    review_session.awaiting_answer = False
    review_session.submitted_answer = None
    next_sentence = await _next_question(session, review_session, await session.get(User, user_id), now)
    if next_sentence is None:
        return None, "complete"
    return (review_session, next_sentence), "graded"


async def reset_review_schedules(session, sentence: Sentence, modes, now=None) -> None:
    """Reset affected modes while retaining attempt history at its old text revision."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    modes = tuple(mode for mode in modes if mode in REVIEW_MODES)
    if not modes:
        return
    cards = (await session.scalars(
        select(ReviewCard)
        .where(ReviewCard.sentence_id == sentence.id, ReviewCard.mode.in_(modes))
        .with_for_update()
    )).all()
    for card in cards:
        card.card_state = new_card(now)
        card.due_at = now
        card.last_review_at = None
        card.first_reviewed_at = None
        card.content_revision = sentence.review_revision
    await session.execute(
        update(ReviewSession)
        .where(
            ReviewSession.current_sentence_id == sentence.id,
            ReviewSession.state == "active",
            ReviewSession.mode.in_(modes),
        )
        .values(
            state="cancelled", current_sentence_id=None, current_audio_asset_id=None,
            awaiting_answer=False, submitted_answer=None, completed_at=now,
        )
    )


async def submit_typed_answer(session, user_id: int, answer: str):
    observed = await session.scalar(
        select(ReviewSession).where(
            ReviewSession.user_id == user_id,
            ReviewSession.state == "active",
            ReviewSession.mode.in_(TYPED_MODES),
            ReviewSession.awaiting_answer.is_(True),
        )
    )
    if observed is None:
        return None
    sentence = await session.scalar(
        select(Sentence).where(
            Sentence.id == observed.current_sentence_id,
            Sentence.user_id == user_id,
            Sentence.state == "ready",
        ).with_for_update()
    )
    review_session = await session.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.id == observed.id,
            ReviewSession.user_id == user_id,
            ReviewSession.state == "active",
            ReviewSession.question_number == observed.question_number,
            ReviewSession.awaiting_answer.is_(True),
            ReviewSession.current_sentence_id == observed.current_sentence_id,
        )
        .with_for_update()
    )
    if (
        sentence is None
        or review_session is None
        or sentence.review_revision != review_session.question_revision
    ):
        return None
    review_session.submitted_answer = answer.strip()[:1000]
    review_session.awaiting_answer = False
    return review_session, sentence


async def cancel_session(session, user_id: int) -> bool:
    active = await session.scalar(
        select(ReviewSession)
        .where(ReviewSession.user_id == user_id, ReviewSession.state == "active")
        .with_for_update()
    )
    if active is None:
        return False
    active.state = "cancelled"
    active.current_sentence_id = None
    active.current_audio_asset_id = None
    active.awaiting_answer = False
    active.submitted_answer = None
    active.completed_at = datetime.now(UTC)
    return True
