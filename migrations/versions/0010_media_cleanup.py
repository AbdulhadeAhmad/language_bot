"""Add reference-aware archived media cleanup jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0010_media_cleanup"
down_revision = "0009_recording_replacement"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "stored_media", sa.Column("delete_pending", sa.Boolean(), nullable=False, server_default="false")
    )
    op.create_table(
        "media_deletion_jobs",
        sa.Column("media_id", UUID(as_uuid=True), sa.ForeignKey("stored_media.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("target_chat_id", sa.BigInteger()),
        sa.Column("target_message_id", sa.BigInteger()),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(100)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", UUID(as_uuid=True)),
        sa.CheckConstraint("state IN ('pending','running','done','failed','cancelled')", name="media_deletion_job_state"),
    )
    op.create_index("ix_media_deletion_jobs_ready", "media_deletion_jobs", ["state", "available_at"])


def downgrade():
    op.drop_index("ix_media_deletion_jobs_ready", table_name="media_deletion_jobs")
    op.drop_table("media_deletion_jobs")
    op.drop_column("stored_media", "delete_pending")
