import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from app.bot.transport import RejectedDelivery, RetryAfter, TelegramTransport
from app.config import Settings
from app.db.models import IncomingUpdate, TelegramDelivery, User
from app.db.session import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    id: int
    token: UUID
    method: str
    payload: dict
    attempts: int


class DeliveryService:
    def __init__(
        self, database: Database, settings: Settings, transport: TelegramTransport
    ) -> None:
        self.database, self.settings, self.transport = database, settings, transport

    async def claim_one(self) -> Claim | None:
        async with self.database.sessions.begin() as session:
            # If a process died during an external call, Telegram may already have accepted it.
            # Record uncertainty instead of blindly sending a second status message.
            await session.execute(
                update(TelegramDelivery)
                .where(
                    TelegramDelivery.state == "sending", TelegramDelivery.lease_until < func.now()
                )
                .values(state="uncertain", claim_token=None, error_type="ExpiredDeliveryLease")
            )
            delivery = await session.scalar(
                select(TelegramDelivery)
                .where(
                    TelegramDelivery.state == "pending", TelegramDelivery.available_at <= func.now()
                )
                .order_by(TelegramDelivery.available_at, TelegramDelivery.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if delivery is None:
                return None
            user = (
                await session.get(User, delivery.actor_id)
                if delivery.audience == "protected"
                else None
            )
            if (
                delivery.audience == "protected"
                and delivery.actor_id != self.settings.admin_telegram_id
                and (user is None or user.status != "approved")
            ):
                delivery.state, delivery.error_type = "failed", "AccessRevoked"
                return None
            if delivery.action_key == "request":
                incoming = await session.get(IncomingUpdate, delivery.update_id)
                target = await session.get(User, incoming.actor_id)
                if target and target.status != "pending":
                    delivery.payload = {
                        "chat_id": self.settings.admin_notification_channel_id,
                        "text": f"User {target.id}: {target.status}",
                    }
            token = uuid4()
            delivery.state, delivery.claim_token = "sending", token
            delivery.attempts += 1
            delivery.lease_until = func.now() + timedelta(
                seconds=self.settings.delivery_lease_seconds
            )
            return Claim(delivery.id, token, delivery.method, delivery.payload, delivery.attempts)

    async def finish(self, claim: Claim, state: str, **values) -> None:
        async with self.database.sessions.begin() as session:
            await session.execute(
                update(TelegramDelivery)
                .where(
                    TelegramDelivery.id == claim.id,
                    TelegramDelivery.claim_token == claim.token,
                    TelegramDelivery.state == "sending",
                )
                .values(state=state, claim_token=None, lease_until=None, **values)
            )

    async def process_one(self) -> bool:
        claim = await self.claim_one()
        if claim is None:
            return False
        try:
            async with asyncio.timeout(self.settings.delivery_timeout_seconds):
                message_id = await self.transport.send(claim.method, claim.payload)
        except RetryAfter as exc:
            if claim.attempts >= self.settings.delivery_max_attempts:
                await self.finish(claim, "failed", error_type="RateLimitExhausted")
            else:
                await self.finish(
                    claim,
                    "pending",
                    error_type="RateLimited",
                    available_at=func.now() + timedelta(seconds=exc.seconds),
                )
            return True
        except RejectedDelivery:
            await self.finish(claim, "failed", error_type="TelegramRejected")
            return True
        except Exception as exc:
            await self.finish(claim, "uncertain", error_type=type(exc).__name__[:100])
            logger.warning(
                "delivery_uncertain",
                extra={
                    "delivery_id": claim.id,
                    "error_type": type(exc).__name__,
                    "operation": "send_reply",
                },
            )
            return True
        await self.finish(claim, "sent", telegram_message_id=message_id, error_type=None)
        logger.info("delivery_sent", extra={"delivery_id": claim.id, "operation": "send_reply"})
        return True
