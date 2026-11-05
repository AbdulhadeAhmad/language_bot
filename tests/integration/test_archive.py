import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.bot.transport import RejectedDelivery, RetryAfter
from app.db.models import ArchiveJob, AudioAsset, Sentence, StoredMedia, User
from app.services.archive import ArchiveRunner, retry_archive

from .test_collections import capture
from .test_foundation import RecordingTransport, dispatch, payload
from .test_operations import VOICE


async def setup_archive(db, config):
    await capture(db, config, RecordingTransport(), voice=VOICE)
    config.telegram_media_channel_id = -10088
    config.telegram_archive_enabled = True
    async with db.sessions() as session:
        job = await session.scalar(select(ArchiveJob))
        sentence = await session.scalar(select(Sentence))
    return job, sentence


async def make_available(db):
    async with db.sessions.begin() as session:
        job = await session.scalar(select(ArchiveJob))
        job.available_at = func.now() - timedelta(seconds=1)


async def test_archive_enqueue_disabled_until_enabled_and_concurrent_claims(db, config):
    job, _ = await setup_archive(db, config)
    transport = RecordingTransport()
    config.telegram_archive_enabled = False
    assert not await ArchiveRunner(db, config, transport).process_one()
    assert transport.sent == []
    async with db.sessions() as session:
        assert (await session.get(ArchiveJob, job.media_id)).attempts == 0
        assert (await session.scalar(select(AudioAsset))).state == "archiving"
    config.telegram_archive_enabled = True
    runner = ArchiveRunner(db, config, transport)
    await asyncio.gather(*(runner.process_one() for _ in range(5)))
    assert len(transport.sent) == 1
    method, data = transport.sent[0]
    assert method == "copy_message" and data["chat_id"] == -10088
    assert data["from_chat_id"] == 11 and data["message_id"] == 1
    assert data["caption"] == "" and data["reply_markup"] == {"inline_keyboard": []}
    async with db.sessions() as session:
        media = await session.get(StoredMedia, job.media_id)
        assert media.archive_chat_id == -10088 and media.archive_message_id == 101
        assert (await session.get(ArchiveJob, job.media_id)).state == "done"
        assert (await session.scalar(select(AudioAsset))).state == "ready"
        assert await session.scalar(select(func.count()).select_from(AudioAsset)) == 1
    assert not await ArchiveRunner(db, config, transport).process_one()


async def test_archive_and_sentence_roll_back_together(db, config):
    from app.db.models import Operation
    from app.services.collections import save_capture
    from app.services.operations import OperationRunner

    from .test_foundation import FakeModels

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(voice=VOICE))
    runner = OperationRunner(db, config, transport, FakeModels())
    await runner.process_one()
    await runner.process_one()
    claimed = await runner.claim()
    async with db.sessions() as session:
        async with session.begin():
            await save_capture(session, claimed)
            await session.flush()
            assert await session.scalar(select(ArchiveJob)) is not None
            await session.rollback()
    async with db.sessions() as session:
        assert await session.scalar(select(Sentence)) is None
        assert await session.scalar(select(ArchiveJob)) is None
        assert await session.scalar(select(StoredMedia)) is None
        assert (await session.get(Operation, claimed.id)).state == "running"


async def test_archive_expired_lease_and_late_response_are_fenced(db, config, caplog):
    job, _ = await setup_archive(db, config)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(RecordingTransport):
        async def send(self, method, data):
            entered.set()
            await release.wait()
            return 999

    task = asyncio.create_task(ArchiveRunner(db, config, Slow()).process_one())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(2):
            async with db.sessions.begin() as session:
                row = await session.scalar(select(ArchiveJob).with_for_update())
                row.lease_until = func.now() - timedelta(seconds=1)
        transport = RecordingTransport()
        await dispatch(db, config, transport, payload(2, text="/library"))
        assert "Your collection" in transport.sent[-1][1]["text"]
        assert await ArchiveRunner(db, config, RecordingTransport()).process_one()
    finally:
        release.set()
        await task
    async with db.sessions() as session:
        media = await session.get(StoredMedia, job.media_id)
        assert media.archive_message_id == 101
        row = await session.get(ArchiveJob, job.media_id)
        assert row.state == "done" and row.uncertain and row.attempts == 2
        assert await session.scalar(select(func.count()).select_from(AudioAsset)) == 1
    assert "archive_result_uncertain" in caplog.text
    assert "archive_orphan_possible" in caplog.text


async def test_rate_limit_respects_retry_after_without_uncertain_receipt(db, config):
    job, _ = await setup_archive(db, config)
    transport = RecordingTransport()
    transport.error = RetryAfter(120)
    runner = ArchiveRunner(db, config, transport)
    assert await runner.process_one()
    async with db.sessions() as session:
        row = await session.get(ArchiveJob, job.media_id)
        assert row.state == "pending" and not row.uncertain and row.attempts == 1
        assert (row.available_at - datetime.now(UTC)).total_seconds() > 100
    assert not await runner.process_one()
    await make_available(db)
    transport.error = None
    assert await runner.process_one()


async def test_uncertain_uploads_retry_bounded_and_manual_retry_is_owner_only(db, config, caplog):
    job, sentence = await setup_archive(db, config)
    transport = RecordingTransport()
    transport.error = TimeoutError()
    runner = ArchiveRunner(db, config, transport)
    for _ in range(3):
        assert await runner.process_one()
        await make_available(db)
    assert not await runner.process_one()
    async with db.sessions.begin() as session:
        row = await session.get(ArchiveJob, job.media_id)
        assert row.state == "failed" and row.uncertain and row.attempts == 3
        assert (await session.scalar(select(AudioAsset))).state == "archive_failed"
        assert (await session.get(StoredMedia, job.media_id)).archive_message_id is None
        assert not await retry_archive(session, 22, sentence.id)
        assert await retry_archive(session, 11, sentence.id)
    transport.error = None
    assert await runner.process_one()
    async with db.sessions() as session:
        assert (await session.get(ArchiveJob, job.media_id)).state == "done"
        assert await session.scalar(select(func.count()).select_from(AudioAsset)) == 1
    assert "archive_result_uncertain" in caplog.text


async def test_rejected_upload_stops_and_retry_callback_checks_owner(db, config):
    job, sentence = await setup_archive(db, config)
    transport = RecordingTransport()
    transport.error = RejectedDelivery("source unavailable")
    assert await ArchiveRunner(db, config, transport).process_one()
    async with db.sessions.begin() as session:
        row = await session.get(ArchiveJob, job.media_id)
        assert row.state == "failed" and row.attempts == 1 and not row.uncertain
        session.add(User(id=22, status="approved"))
    transport.error = None
    await dispatch(db, config, transport, payload(2, text=f"/entry {sentence.id}"))
    assert "archive failed" in transport.sent[-1][1]["text"]
    button = transport.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Retry archive"

    def callback(user):
        return {
            "update_id": 99,
            "callback_query": {
                "id": "retry",
                "chat_instance": "test",
                "data": button["callback_data"],
                "from": {"id": user, "is_bot": False, "first_name": "Test"},
                "message": payload(user=user)["message"],
            },
        }

    await dispatch(db, config, transport, callback(22))
    assert "No failed archive" in transport.sent[-1][1]["text"]
    await dispatch(db, config, transport, callback(11))
    assert "Retrying the archive" in transport.sent[-1][1]["text"]
    async with db.sessions() as session:
        assert (await session.get(ArchiveJob, job.media_id)).state == "pending"


async def test_expired_final_attempt_never_sends_a_fourth_copy(db, config):
    job, _ = await setup_archive(db, config)
    async with db.sessions.begin() as session:
        row = await session.get(ArchiveJob, job.media_id)
        row.state, row.attempts = "running", 3
        row.lease_until = func.now() - timedelta(seconds=1)
    transport = RecordingTransport()
    assert await ArchiveRunner(db, config, transport).process_one()
    assert not transport.sent
    async with db.sessions() as session:
        row = await session.get(ArchiveJob, job.media_id)
        assert row.state == "failed" and row.uncertain
