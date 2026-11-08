from uuid import uuid4

from sqlalchemy import select

from app.db.models import AudioAsset, Sentence, StoredMedia, User
from app.services.audio import playback_payload
from app.services.discovery import search_public

from .test_foundation import RecordingTransport, dispatch, payload


def callback(user, data, message_text=""):
    return {
        "update_id": 220,
        "callback_query": {
            "id": "discovery",
            "chat_instance": "test",
            "data": data,
            "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "message": {**payload(user=user)["message"], "date": 1, "text": message_text},
        },
    }


async def test_discovery_privacy_rechecks_and_copies_are_independent(db, config):
    source_id, original_asset_id, generated_asset_id = uuid4(), uuid4(), uuid4()
    original_media_id, generated_media_id = uuid4(), uuid4()
    async with db.sessions.begin() as session:
        session.add_all([
            User(id=11, status="approved", private=False),
            User(id=22, status="approved", private=True),
        ])
        await session.flush()
        session.add(Sentence(
            id=source_id, user_id=11, german="Guten Morgen!", english="Good morning!",
            normalized_german="Guten Morgen!",
        ))
        await session.flush()
        session.add_all([
            StoredMedia(id=original_media_id, telegram_file_id="original", archive_chat_id=-10088, archive_message_id=800),
            StoredMedia(id=generated_media_id, telegram_file_id="generated", archive_chat_id=-10088, archive_message_id=801),
        ])
        await session.flush()
        session.add_all([
            AudioAsset(id=original_asset_id, sentence_id=source_id, stored_media_id=original_media_id, kind="original", state="ready"),
            AudioAsset(id=generated_asset_id, sentence_id=source_id, stored_media_id=generated_media_id, kind="generated", voice_id="de_DE-thorsten-medium", state="ready"),
            Sentence(id=uuid4(), user_id=11, german="Nicht sichtbar.", english="Private.", normalized_german="Nicht sichtbar."),
        ])

    async with db.sessions() as session:
        found = await search_public(session, "Guten")
        assert [row.id for row in found] == [source_id]
        assert await playback_payload(session, 22, generated_asset_id)

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="/search Guten"))
    result_button = transport.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert result_button["callback_data"] == f"publicentry:{source_id}"
    await dispatch(db, config, transport, callback(22, result_button["callback_data"]))
    public_detail = transport.sent[-2][1]
    assert "Guten Morgen!" in public_detail["text"]
    assert "11" not in public_detail["text"]
    keyboard = public_detail["reply_markup"]["inline_keyboard"]
    assert any(button[0]["callback_data"] == f"playpublic:{original_asset_id}" for button in keyboard)
    assert all("Play" in button[0]["text"] or button[0]["text"] in {"Copy to my collection", "Another random entry"} for button in keyboard)

    await dispatch(db, config, transport, callback(22, f"copy:{source_id}"))
    async with db.sessions() as session:
        copied = await session.scalar(select(Sentence).where(Sentence.user_id == 22))
        copied_assets = (await session.scalars(select(AudioAsset).where(AudioAsset.sentence_id == copied.id))).all()
        copied_user = await session.get(User, 22)
        assert copied.copied_from_sentence_id == source_id
        assert copied_user.private
        assert len(copied_assets) == 1 and copied_assets[0].kind == "generated"
        assert copied_assets[0].stored_media_id == generated_media_id
        assert await playback_payload(session, 22, copied_assets[0].id)

    await dispatch(db, config, transport, payload(300, user=11, text="/privacy"))
    privacy_keyboard = transport.sent[-1][1]["reply_markup"]["inline_keyboard"]
    await dispatch(db, config, transport, callback(11, privacy_keyboard[0][0]["callback_data"]))
    assert transport.sent[-1][1]["text"] == "Privacy updated."
    async with db.sessions() as session:
        assert await playback_payload(session, 22, generated_asset_id) is None
        assert await playback_payload(session, 22, copied_assets[0].id)
        assert await search_public(session, "Guten") == []

    await dispatch(db, config, transport, callback(22, f"publicentry:{source_id}"))
    assert transport.sent[-1][1]["text"] == "Entry unavailable."


async def test_public_search_paginates_and_rechecks_visibility(db, config):
    user_id = 310
    async with db.sessions.begin() as session:
        session.add(User(id=user_id, status="approved", private=False))
        await session.flush()
        session.add_all([
            Sentence(
                id=uuid4(),
                user_id=user_id,
                german=f"Hallo Welt {index}",
                english=f"Hello world {index}",
                normalized_german=f"Hallo Welt {index}",
            )
            for index in range(7)
        ])

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=user_id, text="/search Hallo"))
    first_page = transport.sent[-1][1]
    controls = first_page["reply_markup"]["inline_keyboard"][-1]
    assert controls == [{"text": "Next", "callback_data": "search:1"}]

    await dispatch(
        db, config, transport,
        callback(user_id, "search:1", "Public search: Hallo"),
    )
    second_page = transport.sent[-2][1]
    controls = second_page["reply_markup"]["inline_keyboard"][-1]
    assert [button["callback_data"] for button in controls] == ["search:0"]
    assert len(second_page["reply_markup"]["inline_keyboard"]) == 3

    await dispatch(
        db, config, transport,
        payload(user=user_id, text="/privacy"),
    )
    privacy_controls = transport.sent[-1][1]["reply_markup"]["inline_keyboard"]
    await dispatch(db, config, transport, callback(user_id, privacy_controls[0][0]["callback_data"]))
    await dispatch(
        db, config, transport,
        callback(user_id, "search:1", "Public search: Hallo"),
    )
    assert transport.sent[-2][1]["text"].endswith("No public entries found.")
