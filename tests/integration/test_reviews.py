from uuid import uuid4

from sqlalchemy import func, select

from app.db.models import ReviewAttempt, ReviewCard, ReviewSession, Sentence, User

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
