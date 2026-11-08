"""Persistent single-mode review sessions and fenced grading operations."""

from datetime import UTC, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.db.models import ReviewAttempt, ReviewCard, ReviewSession, Sentence, User
from app.services.scheduling import REVIEW_MODES, ensure_review_cards, review_card

DEFAULT_NEW_CARDS_PER_DAY = 10
DEFAULT_SESSION_LIMIT = 20


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=UTC)


async def _next_question(session, review_session, user, now):
    if review_session.questions_answered >= DEFAULT_SESSION_LIMIT:
        review_session.state = "completed"
        review_session.completed_at = now
        review_session.current_sentence_id = None
        return None

    due = await session.scalar(
        select(ReviewCard)
        .join(Sentence, Sentence.id == ReviewCard.sentence_id)
        .where(
            Sentence.user_id == user.id,
            Sentence.state == "ready",
            ReviewCard.mode == review_session.mode,
            ReviewCard.first_reviewed_at.is_not(None),
            ReviewCard.due_at <= now,
        )
        .order_by(ReviewCard.due_at, Sentence.created_at, Sentence.id)
        .with_for_update(skip_locked=True)
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
        if new_today < DEFAULT_NEW_CARDS_PER_DAY:
            due = await session.scalar(
                select(ReviewCard)
                .join(Sentence, Sentence.id == ReviewCard.sentence_id)
                .where(
                    Sentence.user_id == user.id,
                    Sentence.state == "ready",
                    ReviewCard.mode == review_session.mode,
                    ReviewCard.first_reviewed_at.is_(None),
                    ReviewCard.due_at <= now,
                )
                .order_by(Sentence.created_at, Sentence.id)
                .with_for_update(skip_locked=True)
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
    review_session.question_revision = sentence.revision
    if due.first_reviewed_at is None:
        review_session.new_cards_seen += 1
    return sentence


async def start_session(session, user_id: int, mode: str, now: datetime | None = None):
    if mode not in REVIEW_MODES:
        return None, "invalid_mode"
    now = (now or datetime.now(UTC)).astimezone(UTC)
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.status != "approved":
        return None, "not_approved"

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
    review_session = await session.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.id == session_id,
            ReviewSession.user_id == user_id,
            ReviewSession.state == "active",
            ReviewSession.question_number == question_number,
            ReviewSession.current_sentence_id.is_not(None),
        )
        .with_for_update()
    )
    if review_session is None or review_session.mode not in {"de_to_en", "en_to_de"}:
        return None, "expired"
    sentence = await session.scalar(
        select(Sentence).where(
            Sentence.id == review_session.current_sentence_id,
            Sentence.user_id == user_id,
            Sentence.state == "ready",
            Sentence.revision == review_session.question_revision,
        ).with_for_update()
    )
    if sentence is None:
        review_session.current_sentence_id = None
        return None, "stale"
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
        sentence_revision=sentence.revision,
        rating=rating,
    ))
    card.card_state = next_state
    card.due_at = datetime.fromisoformat(next_state["due"])
    card.last_review_at = now
    if card.first_reviewed_at is None:
        card.first_reviewed_at = now
    review_session.questions_answered += 1
    review_session.current_sentence_id = None
    next_sentence = await _next_question(session, review_session, await session.get(User, user_id), now)
    if next_sentence is None:
        return None, "complete"
    return (review_session, next_sentence), "graded"
