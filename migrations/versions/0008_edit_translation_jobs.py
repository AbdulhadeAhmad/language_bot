"""Queue German text edits for translation before publishing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0008_edit_translation_jobs"
down_revision = "0007_sentence_edits"
branch_labels = depends_on = None


def upgrade():
    op.add_column("interaction_states", sa.Column("sentence_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "sentence_edit_jobs",
        sa.Column("sentence_id", UUID(as_uuid=True), sa.ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("german", sa.String(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(100)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", UUID(as_uuid=True)),
        sa.CheckConstraint("state IN ('pending','running','done','failed','cancelled')", name="sentence_edit_job_state"),
    )
    op.create_index("ix_sentence_edit_jobs_ready", "sentence_edit_jobs", ["state", "available_at"])


def downgrade():
    op.drop_index("ix_sentence_edit_jobs_ready", table_name="sentence_edit_jobs")
    op.drop_table("sentence_edit_jobs")
    op.drop_column("interaction_states", "sentence_revision")
