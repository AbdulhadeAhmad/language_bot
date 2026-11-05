"""Personal entries, media references, and non-unique duplicate lookup."""

import unicodedata

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0004_collections"
down_revision = "0003_operations"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "sentences",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "capture_operation_id", UUID(as_uuid=True), sa.ForeignKey("operations.id"), unique=True
        ),
        sa.Column("german", sa.String(), nullable=False),
        sa.Column("normalized_german", sa.String(), nullable=False),
        sa.Column("english", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("duplicate_warning", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "copied_from_sentence_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sentences.id", ondelete="SET NULL"),
        ),
        sa.Column("status_message_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for column in ("user_id", "normalized_german", "copied_from_sentence_id"):
        op.create_index(f"ix_sentences_{column}", "sentences", [column])
    op.create_table(
        "voices",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
    )
    op.create_table(
        "stored_media",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_file_id", sa.String(), nullable=False),
        sa.Column("archive_chat_id", sa.BigInteger()),
        sa.Column("archive_message_id", sa.BigInteger()),
    )
    op.create_table(
        "audio_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sentence_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sentences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stored_media_id", UUID(as_uuid=True), sa.ForeignKey("stored_media.id")),
        sa.Column("voice_id", sa.String(100), sa.ForeignKey("voices.id")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.CheckConstraint("kind IN ('original','generated')", name="audio_kind"),
        sa.CheckConstraint(
            "(kind = 'original' AND voice_id IS NULL) OR (kind = 'generated' AND voice_id IS NOT NULL)",
            name="audio_voice",
        ),
    )
    op.create_index("ix_audio_assets_sentence_id", "audio_assets", ["sentence_id"])
    op.create_index(
        "uq_active_generated_voice",
        "audio_assets",
        ["sentence_id", "voice_id"],
        unique=True,
        postgresql_where=sa.text("active AND kind = 'generated'"),
    )
    op.bulk_insert(
        sa.table("voices", sa.column("id"), sa.column("provider"), sa.column("model")),
        [
            {
                "id": "de_DE-thorsten-medium",
                "provider": "piper",
                "model": "de_DE-thorsten-medium.onnx",
            }
        ],
    )
    # Legacy captures may belong to the configured admin before their first /start.
    # Missing records stay private and pending; migration never grants access.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO users (id) SELECT DISTINCT user_id FROM operations ON CONFLICT DO NOTHING"
        )
    )
    for row in connection.execute(
        sa.text(
            "SELECT * FROM operations WHERE state='done' AND german IS NOT NULL AND english IS NOT NULL"
        )
    ).mappings():
        connection.execute(
            sa.text(
                "INSERT INTO sentences (id,user_id,capture_operation_id,german,normalized_german,english,status_message_id,created_at) "
                "VALUES (:id,:user_id,:id,:german,:normalized,:english,:status_message_id,:created_at)"
            ),
            {**row, "normalized": " ".join(unicodedata.normalize("NFC", row["german"]).split())},
        )
        if row["source_file_id"]:
            connection.execute(
                sa.text("INSERT INTO stored_media (id,telegram_file_id) VALUES (:id,:file)"),
                {"id": row["id"], "file": row["source_file_id"]},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO audio_assets (id,sentence_id,stored_media_id,kind) VALUES (:id,:id,:id,'original')"
                ),
                {"id": row["id"]},
            )


def downgrade():
    op.drop_table("audio_assets")
    op.drop_table("stored_media")
    op.drop_table("voices")
    op.drop_table("sentences")
