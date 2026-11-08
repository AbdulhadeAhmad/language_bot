from datetime import datetime, timedelta, timezone

import pytest

from app.services.scheduling import DESIRED_RETENTION, new_card, review_card


@pytest.mark.parametrize("rating", [1, 2, 3, 4])
def test_fsrs_adapter_round_trips_each_grade(rating):
    now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
    initial = new_card(now)

    updated, review_log = review_card(initial, rating, now)

    assert review_log["rating"] == rating
    assert updated["last_review"] == now.isoformat()
    assert datetime.fromisoformat(updated["due"]) >= now
    assert DESIRED_RETENTION == 0.9


def test_fsrs_adapter_accepts_other_timezones_and_normalizes_output():
    now = datetime(2026, 11, 8, 14, tzinfo=timezone(timedelta(hours=2)))

    updated, review_log = review_card(new_card(now), 3, now)

    assert updated["last_review"] == "2026-11-08T12:00:00+00:00"
    assert review_log["review_datetime"] == "2026-11-08T12:00:00+00:00"


@pytest.mark.parametrize("rating", [0, 5, True, "3"])
def test_fsrs_adapter_rejects_invalid_grades(rating):
    now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="Rating"):
        review_card(new_card(now), rating, now)


def test_fsrs_adapter_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone"):
        new_card(datetime(2026, 11, 8, 12))
