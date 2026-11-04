"""Short-lived Telegram effects executed directly by the API, never queued in SQL."""

import asyncio
import logging
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.bot.handlers import Handlers
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
        except Exception as exc:
            logger.warning(
                "telegram_effect_failed",
                extra={"user_id": identity[0], "error_type": type(exc).__name__},
            )
            async with database.sessions.begin() as session:
                if key == "request":
                    user = await session.get(User, identity[0])
                    user.approval_notice_state = "uncertain"
                elif key.startswith("operation:"):
                    operation = await session.get(Operation, UUID(key.split(":")[1]))
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
                operation = await session.get(Operation, UUID(key.split(":")[1]))
                operation.status_message_id = message_id
                operation.status_send_state, operation.state = "sent", "pending"
    return "handled"
