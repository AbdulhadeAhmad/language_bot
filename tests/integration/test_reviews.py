from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.db.models import (
    AudioAsset,
    ReviewAttempt,
    ReviewCard,
    ReviewSession,
    Sentence,
    StoredMedia,
    User,
    Voice,
)
from app.services.reviews import reset_review_schedules
from app.services.scheduling import ensure_review_cards, review_card

from .test_foundation import RecordingTransport, dispatch, payload


def review_callback(user, data):
    return {
        "update_id": 830,
        "callback_query": {
            "id": "review",
            "chat_instance": "test",
            "data": data,
            "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "message": {
                **payload(user=user)["message"],
                "date": 1,
                "text": "Practice question",
            },
        },
    }


async def test_translation_flashcard_escapes_html_and_grades_once(db, config):
    user_id, sentence_id = 610, uuid4()
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        session.add(Sentence(
            id=sentence_id,
            user_id=user_id,
            german="<Morgen & Co>",
            english="Good <morning> & you",
            normalized_german="<Morgen & Co>",
        ))

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text="/practice de_to_en"))
    question = transport.sent[-1][1]
    assert question["parse_mode"] == "HTML"
    assert question["text"] == (
        "<b>German → English</b>\n&lt;Morgen &amp; Co&gt;\n\n"
        '<span class="tg-spoiler">Good &lt;morning&gt; &amp; you</span>'
    )
    buttons = question["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in buttons] == ["Again", "Hard", "Good", "Easy"]
    callback_data = buttons[2][0]["callback_data"]
    await dispatch(db, config, transport, review_callback(user_id, callback_data))

    async with db.sessions() as session:
        review_session = await session.scalar(
            select(ReviewSession).where(ReviewSession.user_id == user_id)
        )
        attempt_count = await session.scalar(
            select(func.count()).select_from(ReviewAttempt)
        )
        card = await session.get(ReviewCard, (sentence_id, "de_to_en"))
        assert review_session.state == "completed"
        assert review_session.questions_answered == 1
        assert attempt_count == 1
        assert card.first_reviewed_at is not None

    await dispatch(db, config, transport, review_callback(user_id, callback_data))
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ReviewAttempt)) == 1
    assert transport.sent[-1][1]["text"] == "This review question has expired."


async def test_text_edits_reset_affected_modes_and_preserve_attempt_history(db):
    user_id, sentence_id, session_id = 620, uuid4(), uuid4()
    reviewed_at = datetime(2026, 11, 8, 12, tzinfo=UTC)
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        sentence = Sentence(
            id=sentence_id, user_id=user_id, german="Ich lerne.", english="I am learning.",
            normalized_german="Ich lerne.",
        )
        session.add(sentence)
        await session.flush()
        await ensure_review_cards(session, sentence, reviewed_at)
        await session.flush()
        card = await session.get(ReviewCard, (sentence_id, "de_to_en"))
        state, _ = review_card(card.card_state, 3, reviewed_at)
        card.card_state = state
        card.due_at = reviewed_at.replace(minute=10)
        card.first_reviewed_at = reviewed_at
        card.last_review_at = reviewed_at
        dictation = await session.get(ReviewCard, (sentence_id, "dictation"))
        dictation_state, _ = review_card(dictation.card_state, 3, reviewed_at)
        dictation.card_state = dictation_state
        dictation.due_at = reviewed_at.replace(minute=10)
        dictation.first_reviewed_at = reviewed_at
        dictation.last_review_at = reviewed_at
        session.add(ReviewSession(
            id=session_id, user_id=user_id, mode="de_to_en", state="active",
            current_sentence_id=sentence_id, question_number=1, question_revision=1,
        ))
        await session.flush()
        session.add(ReviewAttempt(
            id=uuid4(), session_id=session_id, sentence_id=sentence_id, mode="de_to_en",
            question_number=1, sentence_revision=1, rating=3,
        ))
        await session.flush()

        sentence.english = "I am studying."
        sentence.review_revision = 2
        await reset_review_schedules(session, sentence, ("de_to_en", "en_to_de", "listening"), reviewed_at)
        assert (await session.get(ReviewSession, session_id)).state == "cancelled"
        assert (await session.scalar(select(func.count()).select_from(ReviewAttempt))) == 1
        for mode in ("de_to_en", "en_to_de", "listening"):
            reset = await session.get(ReviewCard, (sentence_id, mode))
            assert reset.first_reviewed_at is None and reset.last_review_at is None
            assert reset.content_revision == 2 and reset.due_at == reviewed_at
        dictation = await session.get(ReviewCard, (sentence_id, "dictation"))
        assert dictation.first_reviewed_at == reviewed_at
        assert dictation.content_revision == 1

        sentence.german = "Ich studiere."
        sentence.review_revision = 3
        await reset_review_schedules(session, sentence, ("de_to_en", "en_to_de", "dictation", "listening"), reviewed_at)
        dictation = await session.get(ReviewCard, (sentence_id, "dictation"))
        assert dictation.first_reviewed_at is None and dictation.content_revision == 3

    async with db.sessions() as session:
        attempt = await session.scalar(select(ReviewAttempt).where(ReviewAttempt.session_id == session_id))
        assert attempt.sentence_revision == 1 and attempt.rating == 3


async def test_dictation_plays_anonymous_audio_then_records_typed_answer(db, config):
    user_id, sentence_id, asset_id = 630, uuid4(), uuid4()
    media_id = uuid4()
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        session.add_all([
            Sentence(
                id=sentence_id, user_id=user_id, german="Guten Morgen.",
                english="Good morning.", normalized_german="Guten Morgen.",
            ),
            Voice(id="de_DE-test", provider="piper", model="test"),
        ])
        await session.flush()
        session.add(StoredMedia(
            id=media_id, telegram_file_id="private-audio", archive_chat_id=-10088,
            archive_message_id=902,
        ))
        await session.flush()
        session.add(AudioAsset(
            id=asset_id, sentence_id=sentence_id, stored_media_id=media_id,
            voice_id="de_DE-test", kind="generated", state="ready",
        ))

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text="/practice dictation"))
    audio = transport.sent[-2][1]
    instruction = transport.sent[-1][1]
    assert transport.sent[-2][0] == "copy_message"
    assert audio["chat_id"] == user_id and audio["from_chat_id"] == -10088
    assert audio["message_id"] == 902 and audio["caption"] == ""
    assert "Guten Morgen" not in instruction["text"]
    assert instruction["text"] == "Type the German sentence you heard."

    async with db.sessions() as session:
        review_session = await session.scalar(
            select(ReviewSession).where(ReviewSession.user_id == user_id)
        )
        assert review_session.awaiting_answer
        question_number = review_session.question_number
        session_id = review_session.id
    await dispatch(
        db, config, transport,
        review_callback(user_id, f"grade:{session_id.hex}:{question_number}:3"),
    )
    assert transport.sent[-1][1]["text"] == "This review question has expired."

    await dispatch(db, config, transport, payload(user=user_id, text="Guten Morgen."))
    revealed = transport.sent[-1][1]
    assert "Guten Morgen." in revealed["text"]
    assert '<span class="tg-spoiler">Guten Morgen.</span>' in revealed["text"]
    assert [row[0]["text"] for row in revealed["reply_markup"]["inline_keyboard"]] == [
        "Again", "Hard", "Good", "Easy"
    ]
    await dispatch(
        db, config, transport,
        review_callback(user_id, f"grade:{session_id.hex}:{question_number}:3"),
    )
    async with db.sessions() as session:
        attempt = await session.scalar(select(ReviewAttempt).where(ReviewAttempt.session_id == session_id))
        assert attempt.submitted_answer == "Guten Morgen."


async def test_review_limits_are_user_configurable(db, config):
    user_id = 640
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
    transport = RecordingTransport()

    await dispatch(db, config, transport, payload(user=user_id, text="/reviewlimits 0 12"))

    async with db.sessions() as session:
        user = await session.get(User, user_id)
        assert user.new_cards_per_day == 0
        assert user.review_session_limit == 12
    assert transport.sent[-1][1]["text"] == "Review limits saved: 0 new per day, 12 per session."
