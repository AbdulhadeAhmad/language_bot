import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.bot.transport import BotTransport
from app.bot.updates import parse_update
from app.config import Settings
from app.db.session import Database
from app.logging import configure_logging
from app.providers.http import ModelClient
from app.services.archive import ArchiveRunner
from app.services.audio import AudioRunner
from app.services.deletion import MediaDeletionRunner
from app.services.edits import EditRunner
from app.services.operations import OperationRunner
from app.services.reminders import ReminderRunner
from app.services.telegram import handle_update

MAX_UPDATE_BYTES = 512 * 1024
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    *,
    transport=None,
    models=None,
    run_operations=True,
) -> FastAPI:
    settings = settings or Settings()
    owns_database = database is None
    database = database or Database(settings.database_url.get_secret_value())
    transport = transport or BotTransport(settings.telegram_bot_token.get_secret_value())
    models = models or ModelClient(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        tasks = []
        if run_operations:
            tasks.append(
                asyncio.create_task(ReminderRunner(database, settings, transport).serve())
            )
            tasks.append(
                asyncio.create_task(OperationRunner(database, settings, transport, models).serve())
            )
            tasks.append(asyncio.create_task(EditRunner(database, settings, transport, models).serve()))
            tasks.append(asyncio.create_task(MediaDeletionRunner(database, settings, transport).serve()))
            if settings.telegram_archive_enabled:
                tasks.append(
                    asyncio.create_task(ArchiveRunner(database, settings, transport).serve())
                )
            if settings.telegram_tts_enabled:
                tasks.append(
                    asyncio.create_task(AudioRunner(database, settings, transport, models).serve())
                )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await transport.close()
            await models.close()
        if owns_database:
            await database.close()

    app = FastAPI(title="German Learning Bot", lifespan=lifespan)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with asyncio.timeout(3):
                if await database.ready():
                    return {"status": "ready"}
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        raise HTTPException(503, "Database unavailable or migrations not applied")

    @app.post("/telegram/webhook")
    async def webhook(request: Request):
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        expected = settings.telegram_webhook_secret.get_secret_value()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(403, "Invalid webhook secret")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UPDATE_BYTES:
                raise HTTPException(413, "Update too large")
        try:
            payload = json.loads(body)
            parsed = parse_update(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(422, "Invalid Telegram update") from None
        try:
            status = await handle_update(database, settings, transport, parsed)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.error(
                "handler_database_failed",
                extra={"update_id": parsed.update_id, "error_type": type(exc).__name__},
            )
            # Telegram retries database failures; capture uniqueness is by chat/message ID.
            raise HTTPException(503, "Could not handle request") from None
        return {"status": status}

    return app


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8060)
