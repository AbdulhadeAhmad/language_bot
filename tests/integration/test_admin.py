import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.bot.updates import parse_update
from app.db.models import AccessChange, TelegramDelivery, User
from app.services.inbox import InboxService

from .test_foundation import enqueue, payload


def callback(update_id, actor=11, data="access:22:0:approved", chat=-10099):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "chat_instance": "test",
            "from": {"id": actor, "is_bot": False, "first_name": "Admin"},
            "data": data,
            "message": {
                "message_id": 90,
                "date": 0,
                "chat": {"id": chat, "type": "channel" if chat < 0 else "private"},
            },
        },
    }


async def dispatch(db, config, data):
    service = InboxService(db, config)
    result = await service.receive(parse_update(data), data)
    if result == "accepted":
        await service.process_one()
    return result


async def test_registration_duplicate_starts_one_channel_request(db, config):
    services = [await enqueue(db, config, update_id=i, user=22) for i in range(1, 5)]
    await asyncio.gather(*(service.process_one() for service in services))
    async with db.sessions() as session:
        user = await session.get(User, 22)
        assert user.status == "pending" and user.private and user.registered_at
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TelegramDelivery)
                .where(TelegramDelivery.action_key == "request")
            )
            == 1
        )
    updated = payload(5, user=22)
    updated["message"]["from"]["first_name"] = "Changed"
    await dispatch(db, config, updated)
    async with db.sessions() as session:
        assert (await session.get(User, 22)).first_name == "Changed"


async def test_only_admin_can_decide_and_stale_buttons_do_nothing(db, config):
    await dispatch(db, config, payload(user=22))
    await dispatch(db, config, callback(2, actor=33))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "pending"
    assert await dispatch(db, config, callback(3, chat=-999)) == "ignored"
    await dispatch(db, config, callback(4))
    await dispatch(db, config, callback(5, data="access:22:0:rejected"))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "approved"
        assert await session.scalar(select(func.count()).select_from(AccessChange)) == 1
        actions = (
            await session.scalars(select(TelegramDelivery).where(TelegramDelivery.update_id == 4))
        ).all()
        assert {r.action_key for r in actions} == {"decision", "edit", "callback"}


async def test_preapproval_revoke_restore_and_admin_protection(db, config):
    await dispatch(db, config, payload(1, text="/admin grant 22"))
    await dispatch(db, config, payload(2, user=22))
    await dispatch(db, config, callback(3, data="access:22:1:revoked"))
    await dispatch(db, config, payload(4, user=22))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "revoked"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TelegramDelivery)
                .where(TelegramDelivery.action_key == "request")
            )
            == 0
        )
    await dispatch(db, config, callback(5, data="access:22:2:approved"))
    await dispatch(db, config, payload(6))
    await dispatch(db, config, callback(7, data="access:11:0:revoked"))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "approved"
        assert (await session.get(User, 11)).status == "approved"


async def test_notification_failure_does_not_undo_decision(db, config):
    from app.bot.transport import RejectedDelivery
    from app.services.delivery import DeliveryService

    from .test_foundation import RecordingTransport

    await dispatch(db, config, payload(1, user=22))
    await dispatch(db, config, callback(2, data="access:22:0:rejected"))
    transport = RecordingTransport(RejectedDelivery())
    service = DeliveryService(db, config, transport)
    while await service.process_one():
        pass
    await dispatch(db, config, payload(3, user=22))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "rejected"
        assert await session.scalar(select(func.count()).select_from(AccessChange)) == 1


async def test_native_handler_error_rolls_back_registration_and_outbox(db, config):
    from app.bot.handlers import Handlers

    service = await enqueue(db, config, user=22)
    original = Handlers.say

    def fail_after_enqueue(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if kwargs.get("key") == "request":
            raise RuntimeError("interrupt")

    with patch.object(Handlers, "say", fail_after_enqueue):
        with pytest.raises(RuntimeError):
            await service.process_one()
    async with db.sessions() as session:
        assert await session.get(User, 22) is None
        assert await session.scalar(select(func.count()).select_from(TelegramDelivery)) == 0
    assert await service.process_one()


async def test_admin_menu_pagination_and_private_decision_updates_channel(db, config):
    from app.services.delivery import DeliveryService

    from .test_foundation import RecordingTransport

    await dispatch(db, config, payload(1, user=22))
    delivery = DeliveryService(db, config, RecordingTransport())
    while await delivery.process_one():
        pass
    async with db.sessions.begin() as session:
        session.add_all(User(id=n, status="pending") for n in range(30, 36))
    await dispatch(db, config, payload(2, text="/admin"))
    async with db.sessions() as session:
        rows = (
            await session.scalars(select(TelegramDelivery).where(TelegramDelivery.update_id == 2))
        ).all()
        assert len([r for r in rows if r.action_key.startswith("user:")]) == 5
        assert any(r.action_key == "page" for r in rows)
    await dispatch(db, config, callback(3, chat=11, data="list:pending:1"))
    await dispatch(db, config, callback(4, chat=11))
    async with db.sessions() as session:
        row = await session.scalar(
            select(TelegramDelivery).where(
                TelegramDelivery.update_id == 4, TelegramDelivery.action_key == "channel_decision"
            )
        )
        assert row.payload["chat_id"] == -10099 and row.payload["message_id"] == 9001


async def test_pending_request_sent_after_preapproval_has_no_stale_buttons(db, config):
    from app.services.delivery import DeliveryService

    from .test_foundation import RecordingTransport

    await dispatch(db, config, payload(1, user=22))
    await dispatch(db, config, payload(2, text="/admin grant 22"))
    transport = RecordingTransport()
    delivery = DeliveryService(db, config, transport)
    while await delivery.process_one():
        pass
    channel = [data for method, data in transport.sent if data.get("chat_id") == -10099]
    assert len(channel) == 1 and "reply_markup" not in channel[0]
