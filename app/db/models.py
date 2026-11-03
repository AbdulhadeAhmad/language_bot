from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IncomingUpdate(Base):
    __tablename__ = "incoming_updates"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'completed', 'ignored')", name="inbox_state"),
        Index("ix_incoming_pending", "received_at", postgresql_where=text("state = 'pending'")),
    )

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramDelivery(Base):
    __tablename__ = "telegram_deliveries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')", name="delivery_state"
        ),
        CheckConstraint("attempts >= 0", name="delivery_attempts"),
        UniqueConstraint("update_id", "action_key", name="uq_delivery_action"),
        Index("ix_delivery_pending", "available_at", postgresql_where=text("state = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Stable action keys make each outgoing effect idempotent within an update.
    update_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incoming_updates.update_id", ondelete="CASCADE")
    )
    action_key: Mapped[str] = mapped_column(String(100), server_default="reply")
    audience: Mapped[str] = mapped_column(String(20), server_default="protected")
    actor_id: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error_type: Mapped[str | None] = mapped_column(String(100))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','revoked')", name="user_status"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    username: Mapped[str | None] = mapped_column(String)
    language_code: Mapped[str | None] = mapped_column(String)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    private: Mapped[bool] = mapped_column(Boolean, server_default="true")
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    version: Mapped[int] = mapped_column(Integer, server_default="0")


class AccessChange(Base):
    __tablename__ = "access_changes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    admin_id: Mapped[int] = mapped_column(BigInteger)
    previous_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
