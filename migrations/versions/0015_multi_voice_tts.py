"""Add two German Piper voices and persist voice identity in inference jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0015_multi_voice_tts"
down_revision = "0014_daily_reminders"
branch_labels = depends_on = None

VOICE_IDS = ("de_DE-eva_k-x_low", "de_DE-karlsson-low")


def upgrade():
    op.add_column("model_jobs", sa.Column("voice_id", sa.String(length=100), nullable=True))
    op.execute(
        "UPDATE model_jobs SET voice_id = 'de_DE-thorsten-medium' "
        "WHERE kind = 'tts' AND voice_id IS NULL"
    )
    op.execute(
        """
        INSERT INTO voices (id, provider, model) VALUES
            ('de_DE-eva_k-x_low', 'piper', 'de_DE-eva_k-x_low.onnx'),
            ('de_DE-karlsson-low', 'piper', 'de_DE-karlsson-low.onnx')
        ON CONFLICT (id) DO UPDATE
        SET provider = EXCLUDED.provider, model = EXCLUDED.model
        """
    )
    op.create_check_constraint(
        "model_job_voice_kind",
        "model_jobs",
        "(kind = 'tts' AND voice_id IS NOT NULL) OR "
        "(kind <> 'tts' AND voice_id IS NULL)",
    )


def downgrade():
    referenced = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM audio_assets "
            "WHERE voice_id IN ('de_DE-eva_k-x_low', 'de_DE-karlsson-low')"
        )
    ).scalar_one()
    if referenced:
        raise RuntimeError("Remove generated audio for the added voices before downgrading")
    op.drop_constraint("model_job_voice_kind", "model_jobs", type_="check")
    op.drop_column("model_jobs", "voice_id")
    op.execute("DELETE FROM voices WHERE id IN ('de_DE-eva_k-x_low', 'de_DE-karlsson-low')")
