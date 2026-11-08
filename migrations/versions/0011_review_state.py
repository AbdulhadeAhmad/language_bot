"""Persist FSRS cards, review sessions, and idempotent review attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0011_review_state"
down_revision = "0010_media_cleanup"
branch_labels = depends_on = None

MODES = "'de_to_en','en_to_de','dictation','listening'"


def upgrade():
    op.create_table(
        "review_cards",
        sa.Column("sentence_id", UUID(as_uuid=True), sa.ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("mode", sa.String(24), primary_key=True),
        sa.Column("card_state", JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_review_at", sa.DateTime(timezone=True)),
        sa.Column("first_reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"mode IN ({MODES})", name="review_card_mode"),
    )
    op.create_index("ix_review_cards_due", "review_cards", ["mode", "due_at"])

    op.create_table(
        "review_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("current_sentence_id", UUID(as_uuid=True), sa.ForeignKey("sentences.id", ondelete="SET NULL")),
        sa.Column("question_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_revision", sa.Integer()),
        sa.Column("questions_answered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_cards_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"mode IN ({MODES})", name="review_session_mode"),
        sa.CheckConstraint("state IN ('active','completed','cancelled')", name="review_session_state"),
    )
    op.create_index("ix_review_sessions_user_id", "review_sessions", ["user_id"])
    op.create_index(
        "uq_active_review_session_user",
        "review_sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.create_table(
        "review_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("review_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sentence_id", UUID(as_uuid=True), sa.ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("sentence_revision", sa.Integer(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("submitted_answer", sa.String()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"mode IN ({MODES})", name="review_attempt_mode"),
        sa.CheckConstraint("rating BETWEEN 1 AND 4", name="review_attempt_rating"),
        sa.UniqueConstraint("session_id", "question_number", name="uq_review_attempt_question"),
    )
    op.create_index(
        "ix_review_attempts_sentence", "review_attempts", ["sentence_id", "mode", "reviewed_at"]
    )


def downgrade():
    op.drop_index("ix_review_attempts_sentence", table_name="review_attempts")
    op.drop_table("review_attempts")
    op.drop_index("uq_active_review_session_user", table_name="review_sessions")
    op.drop_index("ix_review_sessions_user_id", table_name="review_sessions")
    op.drop_table("review_sessions")
    op.drop_index("ix_review_cards_due", table_name="review_cards")
    op.drop_table("review_cards")
