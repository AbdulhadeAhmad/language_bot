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
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Sentence(Base):
    __tablename__ = "sentences"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    capture_operation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("operations.id"), unique=True
    )
    german: Mapped[str] = mapped_column(String)
    normalized_german: Mapped[str] = mapped_column(String, index=True)
    english: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    review_revision: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[str] = mapped_column(String(16), server_default="ready")
    duplicate_warning: Mapped[bool] = mapped_column(Boolean, server_default="false")
    copied_from_sentence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="SET NULL"), index=True
    )
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InteractionState(Base):
    __tablename__ = "interaction_states"
    __table_args__ = (
        CheckConstraint("kind IN ('sentence_edit','recording_replace')", name="interaction_kind"),
        CheckConstraint("step IN ('german','english','audio')", name="interaction_step"),
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    sentence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE")
    )
    sentence_revision: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(16))
    german: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SentenceEditJob(Base):
    __tablename__ = "sentence_edit_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('pending','running','done','failed','cancelled')", name="sentence_edit_job_state"),
        Index("ix_sentence_edit_jobs_ready", "state", "available_at"),
    )
    sentence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True
    )
    base_revision: Mapped[int] = mapped_column(Integer)
    target_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    german: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class Voice(Base):
    __tablename__ = "voices"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String)


class StoredMedia(Base):
    __tablename__ = "stored_media"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    telegram_file_id: Mapped[str] = mapped_column(String)
    archive_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    archive_message_id: Mapped[int | None] = mapped_column(BigInteger)
    delete_pending: Mapped[bool] = mapped_column(Boolean, server_default="false")


class AudioAsset(Base):
    __tablename__ = "audio_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('original','generated')", name="audio_kind"),
        CheckConstraint(
            "(kind = 'original' AND voice_id IS NULL) OR "
            "(kind = 'generated' AND voice_id IS NOT NULL)",
            name="audio_voice",
        ),
        Index(
            "uq_active_generated_voice",
            "sentence_id",
            "voice_id",
            unique=True,
            postgresql_where="active AND kind = 'generated'",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    sentence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE"), index=True
    )
    stored_media_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stored_media.id")
    )
    voice_id: Mapped[str | None] = mapped_column(String(100), ForeignKey("voices.id"))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), server_default="ready")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    replacement: Mapped[bool] = mapped_column(Boolean, server_default="false")


class ArchiveJob(Base):
    __tablename__ = "archive_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('pending','running','done','failed')", name="archive_job_state"),
        Index("ix_archive_jobs_ready", "state", "available_at"),
    )
    media_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stored_media.id", ondelete="CASCADE"), primary_key=True
    )
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    uncertain: Mapped[bool] = mapped_column(Boolean, server_default="false")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaDeletionJob(Base):
    __tablename__ = "media_deletion_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('pending','running','done','failed','cancelled')", name="media_deletion_job_state"),
        Index("ix_media_deletion_jobs_ready", "state", "available_at"),
    )
    media_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("stored_media.id", ondelete="CASCADE"), primary_key=True
    )
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    target_message_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class AudioJob(Base):
    __tablename__ = "audio_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','running','done','failed','cancelled')", name="audio_job_state"
        ),
        CheckConstraint("step IN ('synthesize','upload')", name="audio_job_step"),
        Index("ix_audio_jobs_ready", "state", "available_at"),
    )
    asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("audio_assets.id", ondelete="CASCADE"), primary_key=True
    )
    sentence_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    step: Mapped[str] = mapped_column(String(16), server_default="synthesize")
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    uncertain: Mapped[bool] = mapped_column(Boolean, server_default="false")
    error_type: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','revoked')", name="user_status"
        ),
        CheckConstraint("new_cards_per_day BETWEEN 0 AND 100", name="user_new_cards_per_day"),
        CheckConstraint("review_session_limit BETWEEN 1 AND 100", name="user_review_session_limit"),
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
    new_cards_per_day: Mapped[int] = mapped_column(Integer, server_default="10")
    review_session_limit: Mapped[int] = mapped_column(Integer, server_default="20")


class ReviewCard(Base):
    __tablename__ = "review_cards"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('de_to_en','en_to_de','dictation','listening')", name="review_card_mode"
        ),
        Index("ix_review_cards_due", "mode", "due_at"),
    )
    sentence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(24), primary_key=True)
    card_state: Mapped[dict] = mapped_column(JSONB)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        CheckConstraint("mode IN ('de_to_en','en_to_de','dictation','listening')", name="review_session_mode"),
        CheckConstraint("state IN ('active','completed','cancelled')", name="review_session_state"),
        Index(
            "uq_active_review_session_user", "user_id", unique=True,
            postgresql_where="state = 'active'",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16), server_default="active")
    current_sentence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="SET NULL")
    )
    current_audio_asset_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("audio_assets.id", ondelete="SET NULL")
    )
    awaiting_answer: Mapped[bool] = mapped_column(Boolean, server_default="false")
    submitted_answer: Mapped[str | None] = mapped_column(String)
    question_number: Mapped[int] = mapped_column(Integer, server_default="0")
    question_revision: Mapped[int | None] = mapped_column(Integer)
    questions_answered: Mapped[int] = mapped_column(Integer, server_default="0")
    new_cards_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewAttempt(Base):
    __tablename__ = "review_attempts"
    __table_args__ = (
        CheckConstraint("mode IN ('de_to_en','en_to_de','dictation','listening')", name="review_attempt_mode"),
        CheckConstraint("rating BETWEEN 1 AND 4", name="review_attempt_rating"),
        UniqueConstraint("session_id", "question_number", name="uq_review_attempt_question"),
        Index("ix_review_attempts_sentence", "sentence_id", "mode", "reviewed_at"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("review_sessions.id", ondelete="CASCADE")
    )
    sentence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sentences.id", ondelete="CASCADE")
    )
    mode: Mapped[str] = mapped_column(String(24))
    question_number: Mapped[int] = mapped_column(Integer)
    sentence_revision: Mapped[int] = mapped_column(Integer)
    rating: Mapped[int] = mapped_column(SmallInteger)
    submitted_answer: Mapped[str | None] = mapped_column(String)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
