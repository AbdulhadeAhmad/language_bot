"""Short-lived Telegram effects executed directly by the API, never queued in SQL."""

import asyncio
import logging
from types import SimpleNamespace

from sqlalchemy import select

from app.bot.handlers import Handlers
from app.bot.transport import RejectedDelivery
from app.bot.updates import actor
from app.db.models import Operation, User

logger = logging.getLogger(__name__)


async def handle_update(database, settings, transport, update):
    identity = actor(update, settings)
    if identity is None:
        return "ignored"
    async with database.sessions.begin() as session:
        handlers = Handlers(
            session, settings, SimpleNamespace(actor_id=identity[0], chat_id=identity[1])
        )
        await handlers.dispatch(update)
    # Commit domain changes before network I/O; no transaction stays open while awaiting Telegram.
    for effect in handlers.effects:
        key = effect["key"]
        try:
            async with asyncio.timeout(settings.telegram_timeout_seconds):
                message_id = await transport.send(effect["method"], effect["payload"])
        except RejectedDelivery as exc:
            logger.warning(
                "telegram_effect_rejected",
                extra={
                    "user_id": identity[0],
                    "effect_key": key,
                    "method": effect["method"],
                    "error_type": type(exc).__name__,
                    "telegram_error_code": exc.telegram_error_code,
                    "telegram_description": str(exc),
                },
            )
            if key in {"entry", "library_page"} and effect["method"] == "edit_message_text":
                payload = effect["payload"]
                fallback = {
                    name: payload[name]
                    for name in ("chat_id", "text", "reply_markup", "parse_mode")
                    if name in payload
                }
                try:
                    async with asyncio.timeout(settings.telegram_timeout_seconds):
                        await transport.send("send_message", fallback)
                except Exception as fallback_exc:
                    logger.warning(
                        "telegram_effect_fallback_failed",
                        extra={
                            "user_id": identity[0],
                            "effect_key": key,
                            "error_type": type(fallback_exc).__name__,
                            "telegram_error_code": getattr(
                                fallback_exc, "telegram_error_code", None
                            ),
                            "telegram_description": str(fallback_exc),
                        },
                    )
            continue
        except Exception as exc:
            logger.warning(
                "telegram_effect_failed",
                extra={
                    "user_id": identity[0],
                    "effect_key": key,
                    "method": effect["method"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            async with database.sessions.begin() as session:
                if key == "request":
                    user = await session.get(User, identity[0])
                    user.approval_notice_state = "uncertain"
                elif key.startswith("operation:"):
                    operation = await session.get(Operation, key.split(":")[1])
                    operation.state, operation.status_send_state = "failed", "uncertain"
                    operation.error_type = "StatusDeliveryUncertain"
            continue
        async with database.sessions.begin() as session:
            if key == "request":
                user = await session.scalar(
                    select(User).where(User.id == identity[0]).with_for_update()
                )
                user.approval_message_id, user.approval_notice_state = message_id, "sent"
            elif key.startswith("operation:"):
                operation = await session.get(Operation, key.split(":")[1])
                operation.status_message_id = message_id
                operation.status_send_state, operation.state = "sent", "pending"
    return "handled"
