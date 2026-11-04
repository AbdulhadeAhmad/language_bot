"""Replace raw update inbox/outbox with domain operations and model jobs.

Existing user and access audit records survive. Historic raw updates/deliveries are
intentionally removed; take a database backup before this irreversible migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0003_operations"
down_revision = "0002_access"
branch_labels = depends_on = None


def work_columns():
    return [
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(100)),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.add_column("users", sa.Column("approval_message_id", sa.BigInteger()))
    op.add_column(
        "users",
        sa.Column("approval_notice_state", sa.String(16), nullable=False, server_default="pending"),
    )
    # Preserve the channel reference without keeping its original update payload.
    op.execute("""UPDATE users u SET approval_message_id=d.telegram_message_id,
        approval_notice_state='sent' FROM telegram_deliveries d JOIN incoming_updates i ON i.update_id=d.update_id
        WHERE i.actor_id=u.id AND d.action_key='request' AND d.state='sent'""")
    op.drop_table("telegram_deliveries")
    op.drop_table("incoming_updates")
    op.create_table(
        "operations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_file_id", sa.String()),
        sa.Column("status_message_id", sa.BigInteger()),
        sa.Column("status_send_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("german", sa.String()),
        sa.Column("english", sa.String()),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="initializing"),
        *work_columns(),
        sa.UniqueConstraint("chat_id", "source_message_id", name="uq_operation_source"),
        sa.CheckConstraint("step IN ('transcribe','translate','complete')", name="operation_step"),
        sa.CheckConstraint(
            "state IN ('initializing','pending','running','failed','done','cancelled')",
            name="operation_state",
        ),
    )
    op.create_index("ix_operations_ready", "operations", ["state", "available_at"])
    op.create_table(
        "model_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        *[
            sa.Column(name, sa.String())
            for name in ("input_text", "input_path", "output_text", "output_path")
        ],
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        *work_columns(),
        sa.CheckConstraint("kind IN ('asr','translation','tts')", name="model_kind"),
        sa.CheckConstraint(
            "state IN ('pending','running','done','failed')", name="model_job_state"
        ),
    )
    op.create_index("ix_model_jobs_ready", "model_jobs", ["kind", "state", "available_at"])


def downgrade():
    raise RuntimeError(
        "Raw updates were intentionally removed; restore a pre-migration backup to downgrade"
    )
