import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from telebot.types import Update

from app.bot.handlers import Handlers
from app.bot.updates import actor, parse_update
from app.db.models import IncomingUpdate
from app.db.session import Database

logger = logging.getLogger(__name__)


class InboxService:
    def __init__(self, database: Database, settings) -> None:
        self.database = database
        self.settings = settings

    async def receive(self, update: Update, payload: dict) -> str:
        identity = actor(update, self.settings)
        if identity is None:
            return "ignored"
        user_id, chat_id = identity
        async with self.database.sessions.begin() as session:
            stored = await session.scalar(
                insert(IncomingUpdate)
                .values(
                    update_id=update.update_id, actor_id=user_id, chat_id=chat_id, payload=payload
                )
                .on_conflict_do_nothing(index_elements=[IncomingUpdate.update_id])
                .returning(IncomingUpdate.update_id)
            )
        logger.info(
            "update_stored" if stored is not None else "update_duplicate",
            extra={
                "user_id": user_id,
                "update_id": update.update_id,
                "operation": "receive_update",
            },
        )
        return "accepted" if stored is not None else "duplicate"

    async def process_one(self) -> bool:
        # Rendering is local and quick. The response and completion commit atomically;
        # a crashed transaction rolls back and releases its row lock for another worker.
        async with self.database.sessions.begin() as session:
            incoming = await session.scalar(
                select(IncomingUpdate)
                .where(IncomingUpdate.state == "pending")
                .order_by(IncomingUpdate.received_at, IncomingUpdate.update_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if incoming is None:
                return False
            await Handlers(session, self.settings, incoming).dispatch(
                parse_update(incoming.payload)
            )
            incoming.state = "completed"
            incoming.completed_at = func.now()
            update_id = incoming.update_id
        logger.info(
            "update_processed", extra={"update_id": update_id, "operation": "process_update"}
        )
        return True
