"""Send opt-in local-time reminders for due review cards."""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from app.db.models import ReviewCard, Sentence, User

logger = logging.getLogger(__name__)


class ReminderRunner:
    def __init__(self, database, settings, transport):
        self.database, self.settings, self.transport = database, settings, transport

    async def run_once(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        async with self.database.sessions() as session:
            user_ids = (
                await session.scalars(
                    select(User.id).where(
                        User.status == "approved", User.reminder_enabled.is_(True)
                    )
                )
            ).all()

        sent = 0
        for user_id in user_ids:
            due_count = await self._claim_due_reminder(user_id, now)
            if due_count is None:
                continue
            try:
                await self.transport.send(
                    "send_message",
                    {
                        "chat_id": user_id,
                        "text": (
                            f"Daily review reminder: {due_count} card(s) are due.\n"
                            "Start a session with /practice de_to_en, /practice en_to_de, "
                            "/practice dictation, or /practice listening."
                        ),
                    },
                )
                sent += 1
            except Exception as exc:
                # The local date was committed before sending, so retries and restarts
                # cannot send a duplicate reminder for this date. Continue for others.
                logger.warning(
                    "daily_reminder_delivery_failed",
                    extra={"user_id": user_id, "error_type": type(exc).__name__},
                )
        return sent

    async def _claim_due_reminder(self, user_id: int, now: datetime) -> int | None:
        async with self.database.sessions.begin() as session:
            user = await session.scalar(
                select(User)
                .where(User.id == user_id)
                .with_for_update(skip_locked=True)
            )
            if user is None or user.status != "approved" or not user.reminder_enabled:
                return None
            try:
                local_now = now.astimezone(ZoneInfo(user.reminder_timezone))
            except ZoneInfoNotFoundError:
                logger.warning("daily_reminder_invalid_timezone", extra={"user_id": user_id})
                return None

            if local_now.strftime("%H:%M") != user.reminder_time.strftime("%H:%M"):
                return None
            if user.last_reminder_local_date == local_now.date():
                return None

            due_count = await session.scalar(
                select(func.count())
                .select_from(ReviewCard)
                .join(Sentence, Sentence.id == ReviewCard.sentence_id)
                .where(
                    Sentence.user_id == user_id,
                    Sentence.state == "ready",
                    ReviewCard.due_at <= now,
                )
            )
            if not due_count:
                return None
            user.last_reminder_local_date = local_now.date()
            return due_count

    async def serve(self):
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("daily_reminder_worker_error", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(20)
