from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import (
    AudioAsset,
    InteractionState,
    ReviewAttempt,
    ReviewCard,
    ReviewSession,
    Sentence,
    SentenceEditJob,
    StoredMedia,
    User,
    Voice,
)
from app.services.edits import EditRunner
from app.services.reviews import grade_question, reset_review_schedules, start_session
from app.services.scheduling import ensure_review_cards, review_card

from .test_foundation import FakeModels, RecordingTransport, dispatch, payload


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


@pytest.mark.parametrize(
    ("mode", "typed_answer", "prompt_text", "expected_answer"),
    [
        ("dictation", "Guten Morgen.", "Type the German sentence you heard.", "Guten Morgen."),
        ("listening", "Good morning.", "Type the English meaning of the German audio.", "Good morning."),
    ],
)
async def test_typed_audio_practice_reveals_and_records_answer(
    db, config, mode, typed_answer, prompt_text, expected_answer
):
    user_id, sentence_id, asset_id = 630, uuid4(), uuid4()
    media_id, voice_id = uuid4(), f"de_DE-test-{mode}"
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        session.add_all([
            Sentence(
                id=sentence_id, user_id=user_id, german="Guten Morgen.",
                english="Good morning.", normalized_german="Guten Morgen.",
            ),
            Voice(id=voice_id, provider="piper", model="test"),
        ])
        await session.flush()
        session.add(StoredMedia(
            id=media_id, telegram_file_id="private-audio", archive_chat_id=-10088,
            archive_message_id=902,
        ))
        await session.flush()
        session.add(AudioAsset(
            id=asset_id, sentence_id=sentence_id, stored_media_id=media_id,
            voice_id=voice_id, kind="generated", state="ready",
        ))

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text=f"/practice {mode}"))
    audio = transport.sent[-2][1]
    instruction = transport.sent[-1][1]
    assert transport.sent[-2][0] == "copy_message"
    assert audio["chat_id"] == user_id and audio["from_chat_id"] == -10088
    assert audio["message_id"] == 902 and audio["caption"] == ""
    assert "Guten Morgen" not in instruction["text"]
    assert instruction["text"] == prompt_text

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

    await dispatch(db, config, transport, payload(user=user_id, text=typed_answer))
    revealed = transport.sent[-1][1]
    assert f"Your answer: {typed_answer}" in revealed["text"]
    assert f'<span class="tg-spoiler">{expected_answer}</span>' in revealed["text"]
    assert [row[0]["text"] for row in revealed["reply_markup"]["inline_keyboard"]] == [
        "Again", "Hard", "Good", "Easy"
    ]
    await dispatch(
        db, config, transport,
        review_callback(user_id, f"grade:{session_id.hex}:{question_number}:3"),
    )
    async with db.sessions() as session:
        attempt = await session.scalar(select(ReviewAttempt).where(ReviewAttempt.session_id == session_id))
        assert attempt.submitted_answer == typed_answer


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


async def test_admin_can_view_review_limits_before_registration(db, config):
    transport = RecordingTransport()

    await dispatch(db, config, transport, payload(user=config.admin_telegram_id, text="/reviewlimits"))

    assert transport.sent[-1][1]["text"].startswith("New cards per day: 10. Session size: 20.")


async def test_english_edit_resets_translation_and_listening_only(db, config):
    user_id, sentence_id = 650, uuid4()
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        sentence = Sentence(
            id=sentence_id, user_id=user_id, german="Ich lerne.", english="I am learning.",
            normalized_german="Ich lerne.",
        )
        session.add(sentence)
        await session.flush()
        await ensure_review_cards(session, sentence, now)
        await session.flush()
        for mode in ("de_to_en", "en_to_de", "dictation", "listening"):
            card = await session.get(ReviewCard, (sentence_id, mode))
            card.card_state, _ = review_card(card.card_state, 3, now)
            card.due_at = now.replace(minute=10)
            card.first_reviewed_at = card.last_review_at = now
        session.add(InteractionState(
            user_id=user_id, kind="sentence_edit", sentence_id=sentence_id,
            sentence_revision=1, step="english",
        ))
        session.add(ReviewSession(
            id=uuid4(), user_id=user_id, mode="de_to_en", state="active",
            current_sentence_id=sentence_id, question_number=1, question_revision=1,
        ))

    await dispatch(db, config, RecordingTransport(), payload(user=user_id, text="I study."))

    async with db.sessions() as session:
        sentence = await session.get(Sentence, sentence_id)
        assert sentence.english == "I study." and sentence.review_revision == 2
        assert await session.get(InteractionState, user_id) is None
        review_session = await session.scalar(
            select(ReviewSession).where(ReviewSession.user_id == user_id)
        )
        assert review_session.state == "cancelled"
        for mode in ("de_to_en", "en_to_de", "listening"):
            card = await session.get(ReviewCard, (sentence_id, mode))
            assert card.first_reviewed_at is None and card.content_revision == 2
        dictation = await session.get(ReviewCard, (sentence_id, "dictation"))
        assert dictation.first_reviewed_at == now and dictation.content_revision == 1


async def test_german_edit_worker_resets_all_modes_after_translation(db, config):
    user_id, sentence_id = 660, uuid4()
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        sentence = Sentence(
            id=sentence_id, user_id=user_id, german="Ich lerne.", english="I am learning.",
            normalized_german="Ich lerne.",
        )
        session.add(sentence)
        await session.flush()
        await ensure_review_cards(session, sentence, now)
        await session.flush()
        for mode in ("de_to_en", "en_to_de", "dictation", "listening"):
            card = await session.get(ReviewCard, (sentence_id, mode))
            card.card_state, _ = review_card(card.card_state, 3, now)
            card.due_at = now.replace(minute=10)
            card.first_reviewed_at = card.last_review_at = now
        session.add(SentenceEditJob(
            sentence_id=sentence_id, base_revision=1, target_revision=2,
            german="Ich studiere.",
        ))

    assert await EditRunner(db, config, RecordingTransport(), FakeModels()).process_one()

    async with db.sessions() as session:
        sentence = await session.get(Sentence, sentence_id)
        assert sentence.german == "Ich studiere." and sentence.review_revision == 2
        for mode in ("de_to_en", "en_to_de", "dictation", "listening"):
            card = await session.get(ReviewCard, (sentence_id, mode))
            assert card.first_reviewed_at is None and card.content_revision == 2


async def test_english_to_german_flashcard_direction(db, config):
    user_id = 670
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        session.add(Sentence(
            id=uuid4(), user_id=user_id, german="Ich lerne.", english="I am learning.",
            normalized_german="Ich lerne.",
        ))

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text="/practice en_to_de"))

    question = transport.sent[-1][1]
    assert question["text"] == (
        "<b>English → German</b>\nI am learning.\n\n"
        '<span class="tg-spoiler">Ich lerne.</span>'
    )
    assert question["parse_mode"] == "HTML"


async def test_due_cards_precede_new_cards_and_session_limit_is_enforced(db):
    user_id, due_id, new_id = 680, uuid4(), uuid4()
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    async with db.sessions.begin() as session:
        session.add(User(
            id=user_id, status="approved", private=True,
            new_cards_per_day=0, review_session_limit=1,
        ))
        await session.flush()
        due_sentence = Sentence(
            id=due_id, user_id=user_id, german="Fälliger Satz.", english="Due sentence.",
            normalized_german="Fälliger Satz.",
        )
        new_sentence = Sentence(
            id=new_id, user_id=user_id, german="Neuer Satz.", english="New sentence.",
            normalized_german="Neuer Satz.",
        )
        session.add_all([due_sentence, new_sentence])
        await session.flush()
        await ensure_review_cards(session, due_sentence, now - timedelta(days=1))
        await ensure_review_cards(session, new_sentence, now)
        await session.flush()
        due_card = await session.get(ReviewCard, (due_id, "de_to_en"))
        due_card.card_state, _ = review_card(
            due_card.card_state, 3, now - timedelta(days=1)
        )
        due_card.due_at = now
        due_card.first_reviewed_at = due_card.last_review_at = now - timedelta(days=1)

        result, status = await start_session(session, user_id, "de_to_en", now)
        assert status == "started" and result[1].id == due_id
        review_session, _ = result
        next_question, status = await grade_question(
            session, user_id, review_session.id, review_session.question_number, 3, now
        )
        assert next_question is None and status == "complete"
        assert (await session.get(ReviewCard, (new_id, "de_to_en"))).first_reviewed_at is None


async def test_active_review_question_resumes_with_same_fence(db, config):
    user_id = 690
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=True))
        await session.flush()
        session.add(Sentence(
            id=uuid4(), user_id=user_id, german="Guten Abend.", english="Good evening.",
            normalized_german="Guten Abend.",
        ))

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text="/practice de_to_en"))
    first_question = transport.sent[-1][1]
    first_callback = first_question["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    await dispatch(db, config, transport, payload(2, user=user_id, text="/practice de_to_en"))
    resumed = transport.sent[-1][1]
    resumed_callback = resumed["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert resumed_callback == first_callback
    assert resumed["text"] == first_question["text"]
    async with db.sessions() as session:
        sessions = (await session.scalars(
            select(ReviewSession).where(ReviewSession.user_id == user_id)
        )).all()
        assert len(sessions) == 1 and sessions[0].state == "active"
