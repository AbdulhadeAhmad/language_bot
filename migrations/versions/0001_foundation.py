"""Durable Telegram inbox and response outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "incoming_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('pending', 'completed', 'ignored')", name="inbox_state"),
    )
    op.create_index(
        "ix_incoming_pending",
        "incoming_updates",
        ["received_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_table(
        "telegram_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "update_id",
            sa.BigInteger(),
            sa.ForeignKey("incoming_updates.update_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True)),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("error_type", sa.String(100)),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed', 'uncertain')", name="delivery_state"
        ),
        sa.CheckConstraint("attempts >= 0", name="delivery_attempts"),
    )
    op.create_index(
        "ix_delivery_pending",
        "telegram_deliveries",
        ["available_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )


def downgrade():
    op.drop_table("telegram_deliveries")
    op.drop_table("incoming_updates")
