import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, update

from app.db.models import (
    AudioAsset,
    AudioJob,
    InteractionState,
    MediaDeletionJob,
    Operation,
    Sentence,
    SentenceEditJob,
    StoredMedia,
    User,
)
from app.services.collections import (
    DUPLICATE_WARNING,
    get_owned_sentence,
    has_duplicate,
    list_sentences,
    normalize_german,
    save_capture,
)
from app.services.deletion import MediaDeletionRunner
from app.services.edits import EditRunner
from app.services.operations import OperationRunner, retry_operation

from .test_foundation import FakeModels, RecordingTransport, dispatch, payload
from .test_operations import VOICE


async def capture(db, config, transport, text="Guten Morgen.", user=11, message_id=1, voice=None):
    await dispatch(db, config, transport, payload(message_id, user=user, text=text, voice=voice))
    runner = OperationRunner(db, config, transport, FakeModels())
    for _ in range(3 if voice else 2):
        assert await runner.process_one()


async def test_capture_saves_independent_entries_and_redelivery_is_idempotent(db, config):
    transport = RecordingTransport()
    await capture(db, config, transport)
    await dispatch(db, config, transport, payload(2, text="Guten Morgen.", message_id=1))
    assert not await OperationRunner(db, config, transport, FakeModels()).process_one()
    await capture(db, config, transport, text="  Guten\t Morgen. ", message_id=3)
    assert DUPLICATE_WARNING in transport.sent[-1][1]["text"]
    async with db.sessions() as session:
        rows = await list_sentences(session, 11)
        assert len(rows) == 2 and rows[0].id != rows[1].id
        assert rows[0].normalized_german == rows[1].normalized_german
        assert (await session.get(User, 11)).private
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    await capture(db, config, transport, user=22)
    assert DUPLICATE_WARNING not in transport.sent[-1][1]["text"]
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Sentence)) == 3
        assert len(await list_sentences(session, 22)) == 1


async def test_private_matches_are_hidden_public_matches_warn_and_self_is_excluded(db, config):
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    await capture(db, config, RecordingTransport(), user=22)
    async with db.sessions.begin() as session:
        other = await session.scalar(select(Sentence))
        assert not await has_duplicate(session, 11, "Guten Morgen.")
        assert not await has_duplicate(session, 22, other.german, exclude_id=other.id)
        (await session.get(User, 22)).private = False
    transport = RecordingTransport()
    await capture(db, config, transport)
    assert DUPLICATE_WARNING in transport.sent[-1][1]["text"]
    async with db.sessions() as session:
        assert not await has_duplicate(session, 11, "guten Morgen.")
        assert not await has_duplicate(session, 11, "Guten Morgen!")
        assert await get_owned_sentence(session, 11, other.id) is None


async def test_voice_capture_retains_original_reference(db, config):
    await capture(db, config, RecordingTransport(), voice=VOICE)
    async with db.sessions() as session:
        sentence = await session.scalar(select(Sentence))
        asset = await session.scalar(select(AudioAsset))
        media = await session.get(StoredMedia, asset.stored_media_id)
        assert asset.sentence_id == sentence.id and asset.kind == "original"
        assert media.telegram_file_id == VOICE["file_id"]
        assert media.archive_message_id is None


async def test_simultaneous_identical_submissions_remain_independent(db, config):
    transport = RecordingTransport()
    await asyncio.gather(
        *(dispatch(db, config, transport, payload(i, text="Ich komme.")) for i in range(1, 5))
    )
    runner = OperationRunner(db, config, transport, FakeModels())
    for _ in range(2):
        await asyncio.gather(*(runner.process_one() for _ in range(4)))
    async with db.sessions() as session:
        rows = await list_sentences(session, 11)
        assert len(rows) == 4
        assert len({row.capture_operation_id for row in rows}) == 4


async def test_completion_retry_and_stale_claim_cannot_duplicate_saved_entry(db, config):
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="Hallo"))
    runner = OperationRunner(db, config, transport, FakeModels())
    await runner.process_one()
    old = await runner.claim()
    async with db.sessions.begin() as session:
        await session.execute(
            update(Operation).values(lease_until=func.now() - timedelta(seconds=1))
        )
    current = await runner.claim()
    async with db.sessions.begin() as session:
        assert await save_capture(session, old) is None
        first = await save_capture(session, current)
    async with db.sessions.begin() as session:
        assert (await save_capture(session, current)).id == first.id
    await runner.finish(current, state="failed")
    async with db.sessions.begin() as session:
        assert await retry_operation(session, current.id, 11)
    assert await runner.process_one()
    async with db.sessions() as session:
        assert len(await list_sentences(session, 11)) == 1


async def test_library_pagination_entry_callbacks_and_access_checks(db, config):
    async with db.sessions.begin() as session:
        session.add_all([User(id=11, status="approved"), User(id=22, status="approved")])
        await session.flush()
        for i in range(7):
            session.add(
                Sentence(
                    id=uuid4(),
                    user_id=11,
                    german=f"Satz {i}",
                    english=f"Sentence {i}",
                    normalized_german=normalize_german(f"Satz {i}"),
                )
            )
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="/library"))
    keyboard = transport.sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 6 and keyboard[-1][0]["text"] == "Next"
    entry_callback = keyboard[0][0]["callback_data"]
    await dispatch(db, config, transport, payload(2, text="/library 1"))
    assert "Page 2" in transport.sent[-1][1]["text"]

    def callback(user, data):
        return {
            "update_id": 100,
            "callback_query": {
                "id": "callback",
                "chat_instance": "test",
                "from": {"id": user, "is_bot": False, "first_name": "Test"},
                "data": data,
                "message": payload(user=user)["message"],
            },
        }

    await dispatch(db, config, transport, callback(11, entry_callback))
    assert "Entry opened." == transport.sent[-1][1]["text"]
    before = len(transport.sent)
    await dispatch(db, config, transport, callback(22, entry_callback))
    assert all("Satz" not in data.get("text", "") for _, data in transport.sent[before:])
    assert transport.sent[-1][1]["text"] == "Entry unavailable."
    await dispatch(db, config, transport, payload(4, user=22, text="/library"))
    assert "No saved entries" in transport.sent[-1][1]["text"]
    async with db.sessions.begin() as session:
        (await session.get(User, 22)).status = "revoked"
    await dispatch(db, config, transport, callback(22, entry_callback))
    assert "Send /start" in transport.sent[-1][1]["text"]
    await dispatch(db, config, transport, payload(5, text="/library -1"))
    assert "Use /library" in transport.sent[-1][1]["text"]


async def test_german_edit_translates_durably_and_revision_fences_audio(db, config):
    config.telegram_tts_enabled = True
    sentence_id, asset_id = uuid4(), uuid4()
    async with db.sessions.begin() as session:
        session.add_all([User(id=11, status="approved"), User(id=22, status="approved")])
        await session.flush()
        session.add_all([
            Sentence(id=sentence_id, user_id=11, german="Guten Morgen", english="Good morning", normalized_german="Guten Morgen"),
        ])
        await session.flush()
        session.add(AudioAsset(id=asset_id, sentence_id=sentence_id, kind="generated", voice_id="de_DE-thorsten-medium", state="generating"))
        session.add(AudioJob(asset_id=asset_id, sentence_revision=1))

    def callback(data, user=11):
        return {
            "update_id": 101,
            "callback_query": {
                "id": "edit-callback", "chat_instance": "test",
                "from": {"id": user, "is_bot": False, "first_name": "Test"},
                "data": data, "message": payload(user=user)["message"],
            },
        }

    transport = RecordingTransport()
    await dispatch(db, config, transport, callback(f"edit:{sentence_id}", user=22))
    assert transport.sent[-1][1]["text"] == "Entry unavailable."
    await dispatch(db, config, transport, callback(f"editen:{sentence_id}"))
    await dispatch(db, config, transport, payload(2, text="Morning, hello!"))
    async with db.sessions() as session:
        sentence = await session.get(Sentence, sentence_id)
        asset, audio_job = await session.get(AudioAsset, asset_id), await session.get(AudioJob, asset_id)
        assert sentence.german == "Guten Morgen" and sentence.english == "Morning, hello!"
        assert sentence.revision == 1 and asset.active and audio_job.state == "pending"
    await dispatch(db, config, transport, callback(f"edit:{sentence_id}"))
    await dispatch(db, config, transport, payload(3, text="/cancel"))
    async with db.sessions() as session:
        assert await session.get(InteractionState, 11) is None
        assert (await session.get(Sentence, sentence_id)).german == "Guten Morgen"
    await dispatch(db, config, transport, callback(f"edit:{sentence_id}"))
    await dispatch(db, config, transport, payload(4, text="Guten Tag!"))
    async with db.sessions() as session:
        assert await session.get(InteractionState, 11) is None
        sentence = await session.get(Sentence, sentence_id)
        job = await session.get(SentenceEditJob, sentence_id)
        assert sentence.german == "Guten Morgen" and sentence.english == "Morning, hello!"
        assert job and job.state == "pending" and job.german == "Guten Tag!"
    await EditRunner(db, config, transport, FakeModels()).process_one()
    async with db.sessions() as session:
        sentence = await session.get(Sentence, sentence_id)
        asset, job = await session.get(AudioAsset, asset_id), await session.get(AudioJob, asset_id)
        edit_job = await session.get(SentenceEditJob, sentence_id)
        assert sentence.german == "Guten Tag!" and sentence.english == "Good morning."
        assert sentence.revision == 2 and not sentence.duplicate_warning
        assert asset.active is False and job.state == "cancelled"
        assert edit_job.state == "done"
        replacement = await session.scalar(select(AudioAsset).where(
            AudioAsset.sentence_id == sentence_id,
            AudioAsset.kind == "generated",
            AudioAsset.active.is_(True),
        ))
        replacement_job = await session.get(AudioJob, replacement.id)
        assert replacement.state == "generating" and replacement_job.state == "pending"
        assert replacement_job.sentence_revision == sentence.revision


async def test_stale_sentence_edit_result_cannot_overwrite_newer_revision(db, config):
    sentence_id = uuid4()
    async with db.sessions.begin() as session:
        session.add(User(id=11, status="approved"))
        await session.flush()
        session.add(Sentence(id=sentence_id, user_id=11, german="Alt", english="Old", normalized_german="Alt"))
        await session.flush()
        session.add(SentenceEditJob(sentence_id=sentence_id, base_revision=1, target_revision=2, german="Neu"))
    async with db.sessions.begin() as session:
        (await session.get(Sentence, sentence_id, with_for_update=True)).revision = 2
    runner = EditRunner(db, config, RecordingTransport(), FakeModels())
    assert await runner.process_one()
    async with db.sessions() as session:
        sentence = await session.get(Sentence, sentence_id)
        job = await session.get(SentenceEditJob, sentence_id)
        assert sentence.german == "Alt" and sentence.english == "Old"
        assert job.state == "cancelled"


async def test_entry_deletion_requires_confirmation_and_cleans_unshared_media(db, config):
    sentence_id, copied_id, media_id, asset_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with db.sessions.begin() as session:
        session.add(User(id=11, status="approved"))
        await session.flush()
        source = Sentence(
            id=sentence_id, user_id=11, german="Weg damit.", english="Delete this.",
            normalized_german="Weg damit.",
        )
        session.add(source)
        await session.flush()
        session.add(Sentence(
            id=copied_id, user_id=11, german="Weg damit.", english="Delete this.",
            normalized_german="Weg damit.", copied_from_sentence_id=sentence_id,
        ))
        session.add(StoredMedia(
            id=media_id, telegram_file_id="archived-audio",
            archive_chat_id=-10088, archive_message_id=900,
        ))
        await session.flush()
        session.add(AudioAsset(
            id=asset_id, sentence_id=sentence_id, stored_media_id=media_id,
            kind="original", state="ready",
        ))

    def callback(data):
        return {
            "update_id": 151,
            "callback_query": {
                "id": "delete-entry", "chat_instance": "test", "data": data,
                "from": {"id": 11, "is_bot": False, "first_name": "Test"},
                "message": payload(user=11)["message"],
            },
        }

    transport = RecordingTransport()
    await dispatch(db, config, transport, callback(f"deleteentry:{sentence_id}"))
    prompt = next(data for method, data in transport.sent if method == "send_message" and "Delete this entry" in data["text"])
    cancel = prompt["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    await dispatch(db, config, transport, callback(cancel))
    async with db.sessions() as session:
        assert await session.get(Sentence, sentence_id)
        assert await session.get(MediaDeletionJob, media_id) is None

    await dispatch(db, config, transport, callback(f"deleteentry:{sentence_id}"))
    prompt = [data for method, data in transport.sent if method == "send_message" and "Delete this entry" in data["text"]][-1]
    confirm = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await dispatch(db, config, transport, callback(confirm))
    async with db.sessions() as session:
        assert await session.get(Sentence, sentence_id) is None
        copied = await session.get(Sentence, copied_id)
        assert copied and copied.copied_from_sentence_id is None
        assert await session.get(MediaDeletionJob, media_id)
    assert await MediaDeletionRunner(db, config, transport).process_one()
    async with db.sessions() as session:
        assert await session.get(StoredMedia, media_id) is None
    assert any(method == "delete_message" and data["message_id"] == 900 for method, data in transport.sent)
