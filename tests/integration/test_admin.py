import asyncio

from sqlalchemy import func, select

from app.db.models import AccessChange, User

from .test_foundation import RecordingTransport, dispatch, payload


def callback(update_id, actor=11, data="access:22:0:approved", chat=-10099):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "chat_instance": "test",
            "from": {"id": actor, "is_bot": False, "first_name": "Admin"},
            "data": data,
            "message": {
                "message_id": 102,
                "date": 0,
                "chat": {"id": chat, "type": "channel" if chat < 0 else "private"},
            },
        },
    }


async def test_concurrent_starts_profile_and_one_approval_notice(db, config):
    transport = RecordingTransport()
    await asyncio.gather(
        *(dispatch(db, config, transport, payload(i, user=22)) for i in range(1, 5))
    )
    assert len([data for method, data in transport.sent if data.get("chat_id") == -10099]) == 1
    async with db.sessions() as session:
        user = await session.get(User, 22)
        assert user.private and user.status == "pending" and user.approval_message_id
    data = payload(5, user=22)
    data["message"]["from"]["first_name"] = "Changed"
    await dispatch(db, config, transport, data)
    async with db.sessions() as session:
        assert (await session.get(User, 22)).first_name == "Changed"


async def test_admin_only_and_stale_callbacks(db, config):
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=22))
    await dispatch(db, config, transport, callback(2, actor=33))
    assert await dispatch(db, config, transport, callback(3, chat=-999)) == "ignored"
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "pending"
    await dispatch(db, config, transport, callback(4))
    await dispatch(db, config, transport, callback(5, data="access:22:0:rejected"))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "approved"
        assert await session.scalar(select(func.count()).select_from(AccessChange)) == 1
    assert any(
        method == "edit_message_text" and data["chat_id"] == -10099
        for method, data in transport.sent
    )


async def test_preapproval_revocation_restore_and_owner_protection(db, config):
    transport = RecordingTransport()
    for data in [
        payload(1, text="/admin grant 22"),
        payload(2, user=22),
        callback(3, data="access:22:1:revoked"),
        payload(4, user=22),
    ]:
        await dispatch(db, config, transport, data)
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "revoked"
    await dispatch(db, config, transport, callback(5, data="access:22:2:approved"))
    await dispatch(db, config, transport, payload(6))
    await dispatch(db, config, transport, callback(7, data="access:11:0:revoked"))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "approved"
        assert (await session.get(User, 11)).status == "approved"


async def test_notification_failure_does_not_revert_access(db, config):
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(user=22))
    transport.error = TimeoutError()
    await dispatch(db, config, transport, callback(2, data="access:22:0:rejected"))
    async with db.sessions() as session:
        assert (await session.get(User, 22)).status == "rejected"


async def test_admin_lists_paginate(db, config):
    async with db.sessions.begin() as session:
        session.add_all(User(id=i) for i in range(20, 28))
    transport = RecordingTransport()
    await dispatch(db, config, transport, payload(text="/admin"))
    assert any("Page 1" in data.get("text", "") for _, data in transport.sent)
    await dispatch(db, config, transport, callback(2, chat=11, data="list:pending:1"))
    assert any("Page 2" in data.get("text", "") for _, data in transport.sent)
