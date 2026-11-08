"""Track review text revisions and typed audio prompt state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0012_review_content_revision"
down_revision = "0011_review_state"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "sentences",
        sa.Column("review_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "review_sessions",
        sa.Column("current_audio_asset_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "review_sessions_current_audio_asset_id_fkey",
        "review_sessions",
        "audio_assets",
        ["current_audio_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "review_sessions", sa.Column("awaiting_answer", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column("review_sessions", sa.Column("submitted_answer", sa.String()))


def downgrade():
    op.drop_column("review_sessions", "submitted_answer")
    op.drop_column("review_sessions", "awaiting_answer")
    op.drop_constraint(
        "review_sessions_current_audio_asset_id_fkey", "review_sessions", type_="foreignkey"
    )
    op.drop_column("review_sessions", "current_audio_asset_id")
    op.drop_column("sentences", "review_revision")
