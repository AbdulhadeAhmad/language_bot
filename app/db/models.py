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
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
    approval_message_id: Mapped[int | None] = mapped_column(BigInteger)
    approval_notice_state: Mapped[str] = mapped_column(String(16), server_default="pending")
    version: Mapped[int] = mapped_column(Integer, server_default="0")


class AccessChange(Base):
    __tablename__ = "access_changes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    admin_id: Mapped[int] = mapped_column(BigInteger)
    previous_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Operation(Base):
    """A user's capture operation, never a Telegram update envelope."""

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("chat_id", "source_message_id", name="uq_operation_source"),
        Index("ix_operations_ready", "state", "available_at"),
        CheckConstraint("step IN ('transcribe','translate','complete')", name="operation_step"),
        CheckConstraint(
            "state IN ('initializing','pending','running','failed','done','cancelled')",
            name="operation_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    source_file_id: Mapped[str | None] = mapped_column(String)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status_send_state: Mapped[str] = mapped_column(String(16), server_default="pending")
    german: Mapped[str | None] = mapped_column(String)
    english: Mapped[str | None] = mapped_column(String)
    step: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), server_default="initializing")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelJob(Base):
    """Each model service owns its durable inference requests."""

    __tablename__ = "model_jobs"
    __table_args__ = (
        Index("ix_model_jobs_ready", "kind", "state", "available_at"),
        CheckConstraint("kind IN ('asr','translation','tts')", name="model_kind"),
        CheckConstraint("state IN ('pending','running','done','failed')", name="model_job_state"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    input_text: Mapped[str | None] = mapped_column(String)
    input_path: Mapped[str | None] = mapped_column(String)
    output_text: Mapped[str | None] = mapped_column(String)
    output_path: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
