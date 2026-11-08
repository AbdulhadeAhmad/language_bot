"""Persist paired sentence text edits."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007_sentence_edits"
down_revision = "0006_audio_jobs"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "interaction_states",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("sentence_id", UUID(as_uuid=True), sa.ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("german", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind = 'sentence_edit'", name="interaction_kind"),
        sa.CheckConstraint("step IN ('german','english')", name="interaction_step"),
    )


def downgrade():
    op.drop_table("interaction_states")
