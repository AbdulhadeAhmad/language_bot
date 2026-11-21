from app.core.idgen import generate_id

from sqlalchemy import select

from app.db.models import AudioAsset, ReviewCard, Sentence, StoredMedia, User
from app.services.audio import playback_payload
from app.services.deletion import delete_sentence
from app.services.discovery import copy_public_sentence, search_public, set_privacy

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
    source_id, original_asset_id, generated_asset_id = generate_id(), generate_id(), generate_id()
    original_media_id, generated_media_id = generate_id(), generate_id()
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
            Sentence(id=generate_id(), user_id=11, german="Nicht sichtbar.", english="Private.", normalized_german="Nicht sichtbar."),
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
        copied_cards = (await session.scalars(
            select(ReviewCard).where(ReviewCard.sentence_id == copied.id)
        )).all()
        copied_user = await session.get(User, 22)
        assert copied.copied_from_sentence_id == source_id
        assert copied_user.private
        assert len(copied_assets) == 1 and copied_assets[0].kind == "generated"
        assert copied_assets[0].stored_media_id == generated_media_id
        assert {card.mode for card in copied_cards} == {
            "de_to_en", "en_to_de", "dictation", "listening"
        }
        assert all(card.first_reviewed_at is None and card.last_review_at is None for card in copied_cards)
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
                id=generate_id(),
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


async def test_repeated_copies_keep_immediate_provenance_and_shared_audio(db):
    source_id, media_id = generate_id(), generate_id()
    async with db.sessions.begin() as session:
        session.add_all([
            User(id=410, status="approved", private=True),
            User(id=420, status="approved", private=True),
        ])
        await session.flush()
        session.add(Sentence(
            id=source_id,
            user_id=410,
            german="Ich lerne Deutsch.",
            english="I am learning German.",
            normalized_german="Ich lerne Deutsch.",
        ))
        await session.flush()
        session.add(StoredMedia(
            id=media_id,
            telegram_file_id="shared-generated",
            archive_chat_id=-10088,
            archive_message_id=840,
        ))
        await session.flush()
        session.add(AudioAsset(
            id=generate_id(), sentence_id=source_id, stored_media_id=media_id,
            kind="generated", state="ready", voice_id="de_DE-thorsten-medium",
        ))
        await set_privacy(session, 410, private=False)
        first = await copy_public_sentence(session, 420, source_id)
        second = await copy_public_sentence(session, 420, source_id)
        assert first.id != second.id
        assert first.copied_from_sentence_id == source_id
        assert second.copied_from_sentence_id == source_id
        assert first.duplicate_warning and second.duplicate_warning
        assert await set_privacy(session, 420, private=False) is False
        third = await copy_public_sentence(session, 410, first.id)
        assert third.copied_from_sentence_id == first.id

        assert await delete_sentence(session, 410, source_id)
        await session.flush()
        assert await delete_sentence(session, 420, first.id)

    async with db.sessions() as session:
        copies = (await session.scalars(
            select(Sentence).where(Sentence.id.in_([first.id, second.id, third.id]))
        )).all()
        assert len(copies) == 2
        assert {row.id for row in copies} == {second.id, third.id}
        assert all(row.copied_from_sentence_id is None for row in copies)
        generated = (await session.scalars(
            select(AudioAsset).where(
                AudioAsset.sentence_id.in_([second.id, third.id]),
                AudioAsset.stored_media_id == media_id,
                AudioAsset.active.is_(True),
            )
        )).all()
        assert len(generated) == 2


async def test_latest_and_random_only_show_public_entries(db, config):
    public_user, private_user = 510, 520
    async with db.sessions.begin() as session:
        session.add_all([
            User(id=public_user, status="approved", private=False),
            User(id=private_user, status="approved", private=True),
        ])
        await session.flush()
        session.add_all([
            Sentence(
                id=generate_id(), user_id=public_user,
                german=f"Öffentlich {index}", english=f"Public {index}",
                normalized_german=f"Öffentlich {index}",
            )
            for index in range(6)
        ] + [Sentence(
            id=generate_id(), user_id=private_user, german="Geheim.", english="Secret.",
            normalized_german="Geheim.",
        )])

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=public_user, text="/latest"))
    first_page = transport.sent[-1][1]
    buttons = first_page["reply_markup"]["inline_keyboard"]
    assert len(buttons) == 6
    assert buttons[-1] == [{"text": "Next", "callback_data": "latest:1"}]
    assert "Geheim" not in first_page["text"]

    await dispatch(
        db, config, transport,
        callback(public_user, "latest:1", "Latest public entries · Page 1"),
    )
    second_page = transport.sent[-2][1]
    assert len(second_page["reply_markup"]["inline_keyboard"]) == 2
    assert second_page["reply_markup"]["inline_keyboard"][-1] == [
        {"text": "Previous", "callback_data": "latest:0"}
    ]

    await dispatch(db, config, transport, payload(user=public_user, text="/random"))
    random_detail = transport.sent[-1][1]
    assert "Geheim." not in random_detail["text"]
    assert "Secret." not in random_detail["text"]
    assert random_detail["text"].startswith("German:\nÖffentlich ")
