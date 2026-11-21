from datetime import datetime, timezone

import pytest

from app.core.idgen import (
    EPOCH,
    decode_base32,
    find_typed_ids,
    format_id,
    generate_id,
    normalize_id,
)


def test_anchor_timestamp_and_generated_id_shape():
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    value = generate_id(now=anchor)
    assert value[:6] == "A00000"
    assert len(value) == 8
    assert value == "A00000" + value[6:]
    assert decode_base32("A00000") == int((anchor - EPOCH).total_seconds())


def test_typed_ids_round_trip_and_find_in_text():
    body = generate_id()
    tagged = format_id("sentence", body)
    assert tagged == f"#ST_{body}"
    assert normalize_id(tagged, expected_kind="sentence") == body
    assert find_typed_ids(f"Open {tagged}.") == [("sentence", body)]
    with pytest.raises(ValueError):
        normalize_id(tagged, expected_kind="operation")


def test_old_uuid_is_not_accepted():
    with pytest.raises(ValueError):
        normalize_id("d9ab2e30-55ee-4aa8-9e34-22b08e8d59ee")
    assert find_typed_ids("#ST_d9ab2e30-55ee-4aa8-9e34-22b08e8d59ee") == []
