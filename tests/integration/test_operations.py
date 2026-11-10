import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.models import Operation, User
from app.services.operations import OperationRunner, StaleOperationClaim

from .test_foundation import FakeModels, RecordingTransport, dispatch, payload

VOICE = {"file_id": "telegram-file", "file_unique_id": "unique", "duration": 10, "file_size": 100}


async def test_voice_pipeline_reuses_status_and_persists_only_needed_fields(db, config):
    transport = RecordingTransport()
    await asyncio.gather(
        *(
            dispatch(db, config, transport, payload(i, voice=VOICE, message_id=1))
            for i in range(1, 6)
        )
    )
    assert len(transport.sent) == 1 and transport.sent[0][1]["text"] == "Transcribing…"
    runner = OperationRunner(db, config, transport, FakeModels())
    assert await runner.process_one()
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        assert row.german == "Guten Morgen." and row.step == "translate"
        assert row.source_file_id == "telegram-file"
        assert not hasattr(row, "payload")
    # A new API process can resume the next step from PostgreSQL.
    runner = OperationRunner(db, config, transport, FakeModels())
    assert await runner.process_one()
    assert await runner.process_one()
    assert not await runner.process_one()
    edits = [data for method, data in transport.sent if method == "edit_message_text"]
    assert {data["message_id"] for data in edits} == {101}
    assert edits[-1]["text"] == "German:\nGuten Morgen.\n\nEnglish:\nGood morning."
    async with db.sessions() as session:
        assert (await session.scalar(select(Operation))).state == "done"


async def test_expired_claim_fences_stale_result(db, config):
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="Guten Morgen."))
    runner = OperationRunner(db, config, transport, FakeModels())
    old = await runner.claim()
    async with db.sessions.begin() as session:
        await session.execute(
            update(Operation).values(lease_until=func.now() - timedelta(seconds=1))
        )
    new = await runner.claim()
    assert old.claim_token != new.claim_token
    before = len(transport.sent)
    with pytest.raises(StaleOperationClaim):
        await runner.render(old, "Obsolete progress")
    assert len(transport.sent) == before
    assert not await runner.finish(old, state="done")
    assert await runner.finish(new, state="pending")


async def test_concurrent_claim_and_revoked_access(db, config):
    transport = RecordingTransport()
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    await dispatch(db, config, transport, payload(user=22, voice=VOICE))
    async with db.sessions.begin() as session:
        (await session.get(User, 22)).status = "revoked"
    runner = OperationRunner(db, config, transport, FakeModels())
    await asyncio.gather(*(runner.process_one() for _ in range(5)))
    async with db.sessions() as session:
        assert (await session.scalar(select(Operation))).state == "cancelled"


async def test_failed_status_send_requires_explicit_retry_no_raw_replay(db, config):
    transport = RecordingTransport()
    transport.error = TimeoutError()
    await dispatch(db, config, transport, payload(voice=VOICE))
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        operation_id = row.id
        assert row.state == "failed" and row.status_send_state == "uncertain"
    transport.error = None
    await dispatch(db, config, transport, payload(2, text=f"/retry {operation_id}"))
    async with db.sessions() as session:
        row = await session.get(Operation, operation_id)
        assert row.state == "pending" and row.status_message_id == 101 and row.generation == 1


async def test_retry_ownership_and_input_limits(db, config):
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(voice={**VOICE, "duration": 121}))
    await dispatch(db, config, transport, payload(2, text="x" * 1001))
    async with db.sessions() as session:
        assert await session.scalar(select(Operation)) is None
    await dispatch(db, config, transport, payload(3, text="Guten Morgen."))
    async with db.sessions.begin() as session:
        row = await session.scalar(select(Operation))
        row.state = "failed"
        operation_id = row.id
        session.add(User(id=22, status="approved"))
    await dispatch(db, config, transport, payload(4, user=22, text=f"/retry {operation_id}"))
    async with db.sessions() as session:
        assert (await session.get(Operation, operation_id)).state == "failed"


async def test_network_retry_keeps_successful_previous_stage(db, config):
    class Broken(FakeModels):
        async def translate(self, operation):
            raise OSError("offline")

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(voice=VOICE))
    runner = OperationRunner(db, config, transport, Broken())
    await runner.process_one()
    await runner.process_one()
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        assert row.german == "Guten Morgen." and row.step == "translate" and row.state == "pending"


async def test_model_disconnect_keeps_operation_recoverable_after_retry_limit(db, config):
    class Disconnected(FakeModels):
        async def transcribe(self, operation, transport):
            raise httpx.ConnectError("model service restarting")

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(voice=VOICE))
    async with db.sessions.begin() as session:
        row = await session.scalar(select(Operation))
        row.attempts = 3

    assert await OperationRunner(db, config, transport, Disconnected()).process_one()
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        assert row.state == "pending" and row.error_type == "ConnectError"
        assert row.attempts == 4


async def test_long_voice_result_keeps_full_text_and_offers_owner_only_retrieval(db, config):
    class Long(FakeModels):
        async def transcribe(self, operation, transport):
            return "Guten Morgen. " * 200

        async def translate(self, operation):
            return "Good morning. " * 200

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(voice=VOICE))
    runner = OperationRunner(db, config, transport, Long())
    for _ in range(3):
        await runner.process_one()
    final = transport.sent[-1][1]["text"]
    assert len(final) < 4096 and "Full text: /result " in final
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        operation_id = row.id
        assert row.state == "done" and len(row.german) > 1000 and len(row.english) > 2500
    before = len(transport.sent)
    await dispatch(db, config, transport, payload(2, text=f"/result {operation_id}"))
    assert (
        "".join(data["text"] for _, data in transport.sent[before:])
        == f"German:\n{row.german}\n\nEnglish:\n{row.english}"
    )
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    await dispatch(db, config, transport, payload(3, user=22, text=f"/result {operation_id}"))
    assert "No completed operation" in transport.sent[-1][1]["text"]


async def test_deleted_status_requires_replacement_only_on_explicit_retry(db, config):
    from app.bot.transport import MissingStatusMessage

    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="Hallo"))
    transport.error = MissingStatusMessage()
    runner = OperationRunner(db, config, transport, FakeModels())
    await runner.process_one()
    async with db.sessions() as session:
        row = await session.scalar(select(Operation))
        operation_id = row.id
        assert row.state == "failed" and row.status_message_id is None
    transport.error = None
    await dispatch(db, config, transport, payload(2, text=f"/retry {operation_id}"))
    async with db.sessions() as session:
        assert (await session.get(Operation, operation_id)).status_message_id is not None
