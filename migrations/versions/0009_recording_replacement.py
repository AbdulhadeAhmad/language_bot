"""Support staged replacement recordings."""

import sqlalchemy as sa
from alembic import op

revision = "0009_recording_replacement"
down_revision = "0008_edit_translation_jobs"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("interaction_kind", "interaction_states", type_="check")
    op.create_check_constraint(
        "interaction_kind", "interaction_states", "kind IN ('sentence_edit','recording_replace')"
    )
    op.drop_constraint("interaction_step", "interaction_states", type_="check")
    op.create_check_constraint(
        "interaction_step", "interaction_states", "step IN ('german','english','audio')"
    )
    op.add_column(
        "audio_assets", sa.Column("replacement", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade():
    op.drop_column("audio_assets", "replacement")
    op.drop_constraint("interaction_step", "interaction_states", type_="check")
    op.create_check_constraint("interaction_step", "interaction_states", "step IN ('german','english')")
    op.drop_constraint("interaction_kind", "interaction_states", type_="check")
    op.create_check_constraint("interaction_kind", "interaction_states", "kind = 'sentence_edit'")
