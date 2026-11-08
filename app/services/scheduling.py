"""FSRS adapter kept separate from Telegram and persistence details."""

from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

DESIRED_RETENTION = 0.9
_RATINGS = {
    1: Rating.Again,
    2: Rating.Hard,
    3: Rating.Good,
    4: Rating.Easy,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Review timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def new_card(now: datetime) -> dict:
    """Return a due-now py-fsrs card serialized for JSONB storage."""
    return Card(due=_utc(now)).to_dict()


def review_card(card_state: dict, rating: int, now: datetime) -> tuple[dict, dict]:
    """Apply one self-grade and return JSON-safe card and review-log snapshots."""
    if type(rating) is not int or rating not in _RATINGS:
        raise ValueError("Rating must be an integer from 1 through 4")
    card = Card.from_dict(card_state)
    updated, log = Scheduler(desired_retention=DESIRED_RETENTION).review_card(
        card,
        _RATINGS[rating],
        review_datetime=_utc(now),
    )
    return updated.to_dict(), log.to_dict()
