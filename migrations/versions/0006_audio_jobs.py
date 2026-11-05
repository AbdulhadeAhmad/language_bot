"""On-demand generated speech: durable synthesis/upload stages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0006_audio_jobs"
down_revision = "0005_archive_jobs"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "audio_jobs",
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("audio_assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sentence_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_chat_id", sa.BigInteger()),
        sa.Column("step", sa.String(16), nullable=False, server_default="synthesize"),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_type", sa.String(100)),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','done','failed','cancelled')", name="audio_job_state"
        ),
        sa.CheckConstraint("step IN ('synthesize','upload')", name="audio_job_step"),
    )
    op.create_index("ix_audio_jobs_ready", "audio_jobs", ["state", "available_at"])


def downgrade():
    op.execute("UPDATE audio_assets SET active=false WHERE kind='generated' AND state!='ready'")
    op.drop_table("audio_jobs")
