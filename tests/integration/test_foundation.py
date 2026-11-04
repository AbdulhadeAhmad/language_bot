import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select

from app.bot.updates import parse_update
from app.db.models import User
from app.main import create_app
from app.services.telegram import handle_update


def payload(update_id=1, user=11, text="/start", message_id=None, voice=None):
    message = {
        "message_id": message_id or update_id,
        "date": 0,
        "from": {"id": user, "is_bot": False, "first_name": "Test"},
        "chat": {"id": user, "type": "private"},
    }
    if voice:
        message["voice"] = voice
    else:
        message["text"] = text
    return {"update_id": update_id, "message": message}


class RecordingTransport:
    def __init__(self):
        self.sent = []
        self.error = None

    async def send(self, method, data):
        if self.error:
            raise self.error
        self.sent.append((method, data))
        return len(self.sent) + 100

    async def download(self, file_id):
        return b"test audio"

    async def close(self):
        pass


class FakeModels:
    async def transcribe(self, operation, transport):
        return "Guten Morgen."

    async def translate(self, operation):
        return "Good morning."

    async def close(self):
        pass


async def dispatch(db, config, transport, data):
    return await handle_update(db, config, transport, parse_update(data))


async def test_webhook_commands_run_in_api_without_raw_storage(db, config):
    transport = RecordingTransport()
    app = create_app(config, db, transport=transport, models=FakeModels(), run_operations=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/telegram/webhook", json=payload())).status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": "a" * 32}
        assert (await client.post("/telegram/webhook", json=payload(), headers=headers)).json() == {
            "status": "handled"
        }
        assert (
            await client.post("/telegram/webhook", content="bad", headers=headers)
        ).status_code == 422
        assert (await client.get("/health/ready")).status_code == 200
    assert len(transport.sent) == 1
    async with db.sessions() as session:
        assert (await session.get(User, 11)).status == "approved"
    async with db.engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert "incoming_updates" not in tables and "telegram_deliveries" not in tables


async def test_slow_telegram_call_does_not_hold_database_transaction(db, config):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(RecordingTransport):
        async def send(self, method, data):
            entered.set()
            await release.wait()
            return 99

    task = asyncio.create_task(dispatch(db, config, Slow(), payload()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        async with asyncio.timeout(2):
            async with db.sessions.begin() as session:
                user = await session.scalar(select(User).where(User.id == 11).with_for_update())
                assert user
            await dispatch(db, config, RecordingTransport(), payload(2, user=22))
    finally:
        release.set()
        await task
