"""Durable original-recording archive queue, including existing personal entries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0005_archive_jobs"
down_revision = "0004_collections"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "archive_jobs",
        sa.Column(
            "media_id",
            UUID(as_uuid=True),
            sa.ForeignKey("stored_media.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("target_chat_id", sa.BigInteger()),
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
            "state IN ('pending','running','done','failed')", name="archive_job_state"
        ),
    )
    op.create_index("ix_archive_jobs_ready", "archive_jobs", ["state", "available_at"])
    op.execute("""INSERT INTO archive_jobs (media_id, source_chat_id, source_message_id)
        SELECT DISTINCT ON (m.id) m.id, o.chat_id, o.source_message_id
        FROM stored_media m JOIN audio_assets a ON a.stored_media_id=m.id
        JOIN sentences s ON s.id=a.sentence_id JOIN operations o ON o.id=s.capture_operation_id
        WHERE a.kind='original' AND a.active AND m.archive_message_id IS NULL
        ORDER BY m.id, s.created_at""")
    op.execute("""UPDATE audio_assets SET state='archiving' WHERE stored_media_id IN
        (SELECT media_id FROM archive_jobs) AND active""")


def downgrade():
    op.execute(
        "UPDATE audio_assets SET state='ready' WHERE state IN ('archiving','archive_failed')"
    )
    op.drop_table("archive_jobs")
