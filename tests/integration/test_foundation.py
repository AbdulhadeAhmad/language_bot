import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.bot.transport import RejectedDelivery, RetryAfter
from app.bot.updates import parse_update
from app.db.models import IncomingUpdate, TelegramDelivery, User
from app.main import MAX_UPDATE_BYTES, create_app
from app.services.delivery import DeliveryService
from app.services.inbox import InboxService


def payload(update_id=1, user=11, text="/start"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user, "type": "private"},
            "text": text,
        },
    }


async def enqueue(db, config, **kwargs):
    service = InboxService(db, config)
    data = payload(**kwargs)
    await service.receive(parse_update(data), data)
    return service


class RecordingTransport:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, method, data):
        self.sent.append((method, data))
        if self.error:
            raise self.error
        return 9001

    async def close(self):
        pass


async def test_migrated_database_ready_and_webhook_is_durable(db, config):
    assert await db.ready()
    app = create_app(config, db)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 200
            response = await client.post(
                "/telegram/webhook",
                json=payload(),
                headers={"X-Telegram-Bot-Api-Secret-Token": "a" * 32},
            )
            assert response.json() == {"status": "accepted"}
    async with db.sessions() as session:
        row = await session.get(IncomingUpdate, 1)
        assert row.state == "pending" and row.payload == payload()
        assert await session.scalar(select(func.count()).select_from(TelegramDelivery)) == 0


async def test_authentication_and_allowlist_create_no_work(db, config):
    app = create_app(config, db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for secret in ("", "wrong"):
            assert (
                await client.post(
                    "/telegram/webhook",
                    json=payload(),
                    headers={"X-Telegram-Bot-Api-Secret-Token": secret},
                )
            ).status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": "a" * 32}
        assert (
            await client.post("/telegram/webhook", json={"update_id": 9}, headers=headers)
        ).json() == {"status": "ignored"}
        assert (
            await client.post("/telegram/webhook", content="bad json", headers=headers)
        ).status_code == 422
        assert (
            await client.post(
                "/telegram/webhook", content=b"x" * (MAX_UPDATE_BYTES + 1), headers=headers
            )
        ).status_code == 413
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(IncomingUpdate)) == 0


async def test_concurrent_webhook_redeliveries_have_one_logical_response(db, config):
    service = InboxService(db, config)
    data = payload()
    statuses = await asyncio.gather(*[service.receive(parse_update(data), data) for _ in range(12)])
    assert statuses.count("accepted") == 1 and statuses.count("duplicate") == 11
    processed = await asyncio.gather(*[service.process_one() for _ in range(8)])
    assert processed.count(True) == 1
    transport = RecordingTransport()
    deliveries = DeliveryService(db, config, transport)
    await asyncio.gather(*[deliveries.process_one() for _ in range(8)])
    assert len(transport.sent) == 1
    assert transport.sent[0][1]["chat_id"] == 11
    async with db.sessions() as session:
        row = await session.scalar(select(TelegramDelivery))
        assert row.state == "sent" and row.telegram_message_id == 9001


async def test_handler_failure_rolls_back_for_retry(db, config):
    service = await enqueue(db, config)
    with patch("app.services.inbox.Handlers.dispatch", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError):
            await service.process_one()
    async with db.sessions() as session:
        assert (await session.get(IncomingUpdate, 1)).state == "pending"
        assert await session.scalar(select(func.count()).select_from(TelegramDelivery)) == 0
    # A fresh service instance can resume after the failed transaction/process.
    assert await InboxService(db, config).process_one()


async def test_revoked_access_is_rechecked_before_delivery(db, config):
    async with db.sessions.begin() as session:
        session.add(User(id=22, status="approved"))
    service = await enqueue(db, config, user=22, text="hello")
    await service.process_one()
    async with db.sessions.begin() as session:
        (await session.get(User, 22)).status = "revoked"
    transport = RecordingTransport()
    assert not await DeliveryService(db, config, transport).process_one()
    assert not transport.sent
    async with db.sessions() as session:
        assert (await session.scalar(select(TelegramDelivery))).error_type == "AccessRevoked"


async def test_rate_limit_is_deferred_then_delivered_once(db, config):
    service = await enqueue(db, config)
    await service.process_one()
    transport = RecordingTransport(RetryAfter(60))
    delivery = DeliveryService(db, config, transport)
    assert await delivery.process_one()
    assert not await delivery.process_one()
    async with db.sessions.begin() as session:
        row = await session.scalar(select(TelegramDelivery))
        assert row.state == "pending" and row.attempts == 1
        row.available_at = func.now() - timedelta(seconds=1)
    transport.error = None
    assert await delivery.process_one()
    assert not await delivery.process_one()


@pytest.mark.parametrize(
    "error,state", [(TimeoutError(), "uncertain"), (RejectedDelivery(), "failed")]
)
async def test_failed_external_delivery_is_not_blindly_repeated(db, config, error, state):
    service = await enqueue(db, config)
    await service.process_one()
    transport = RecordingTransport(error)
    delivery = DeliveryService(db, config, transport)
    assert await delivery.process_one()
    assert not await delivery.process_one()
    async with db.sessions() as session:
        assert (await session.scalar(select(TelegramDelivery))).state == state
    assert len(transport.sent) == 1


async def test_expired_delivery_and_stale_completion_cannot_resend(db, config):
    service = await enqueue(db, config)
    await service.process_one()
    transport = RecordingTransport()
    delivery = DeliveryService(db, config, transport)
    claim = await delivery.claim_one()
    assert claim is not None
    async with db.sessions.begin() as session:
        await session.execute(
            update(TelegramDelivery).values(lease_until=func.now() - timedelta(seconds=1))
        )
    assert not await delivery.process_one()
    await delivery.finish(claim, "sent", telegram_message_id=99)
    async with db.sessions() as session:
        assert (await session.scalar(select(TelegramDelivery))).state == "uncertain"
    assert not transport.sent


async def test_callback_is_persisted_and_answered(db, config):
    data = {
        "update_id": 9,
        "callback_query": {
            "id": "callback-9",
            "chat_instance": "test",
            "from": {"id": 11, "is_bot": False, "first_name": "Test"},
            "data": "future-action",
            "message": {"message_id": 1, "date": 0, "chat": {"id": 11, "type": "private"}},
        },
    }
    service = InboxService(db, config)
    assert await service.receive(parse_update(data), data) == "accepted"
    await service.process_one()
    transport = RecordingTransport()
    await DeliveryService(db, config, transport).process_one()
    assert transport.sent[0][0] == "answer_callback_query"
    assert transport.sent[0][1]["callback_query_id"] == "callback-9"


async def test_slow_delivery_does_not_block_other_updates(db, config):
    service = await enqueue(db, config)
    await service.process_one()
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowTransport(RecordingTransport):
        async def send(self, method, data):
            entered.set()
            await release.wait()
            return 1

    task = asyncio.create_task(DeliveryService(db, config, SlowTransport()).process_one())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(2):
            await enqueue(db, config, update_id=2, user=22)
            assert await service.process_one()
    finally:
        release.set()
        await task
