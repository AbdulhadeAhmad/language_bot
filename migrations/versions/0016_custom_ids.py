"""Replace internal UUIDs with compact typed-ID bodies; old IDs are discarded."""

import sqlalchemy as sa
from alembic import op

from app.core.idgen import generate_id

revision = "0016_custom_ids"
down_revision = "0015_multi_voice_tts"
branch_labels = depends_on = None

ENTITY_TABLES = {
    "sentences": "sentence",
    "operations": "operation",
    "stored_media": "media",
    "audio_assets": "audio",
    "review_sessions": "review_session",
    "review_attempts": "review_attempt",
    "model_jobs": "model_job",
}
COLUMN_KINDS = {
    ("sentences", "id"): "sentence",
    ("sentences", "capture_operation_id"): "operation",
    ("sentences", "copied_from_sentence_id"): "sentence",
    ("operations", "id"): "operation",
    ("stored_media", "id"): "media",
    ("audio_assets", "id"): "audio",
    ("audio_assets", "sentence_id"): "sentence",
    ("audio_assets", "stored_media_id"): "media",
    ("archive_jobs", "media_id"): "media",
    ("audio_jobs", "asset_id"): "audio",
    ("interaction_states", "sentence_id"): "sentence",
    ("sentence_edit_jobs", "sentence_id"): "sentence",
    ("media_deletion_jobs", "media_id"): "media",
    ("review_cards", "sentence_id"): "sentence",
    ("review_sessions", "id"): "review_session",
    ("review_sessions", "current_sentence_id"): "sentence",
    ("review_sessions", "current_audio_asset_id"): "audio",
    ("review_attempts", "id"): "review_attempt",
    ("review_attempts", "session_id"): "review_session",
    ("review_attempts", "sentence_id"): "sentence",
    ("model_jobs", "id"): "model_job",
}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    uuid_columns = {}
    for table in inspector.get_table_names():
        columns = [c for c in inspector.get_columns(table) if c["type"].__class__.__name__.lower() == "uuid"]
        if columns:
            uuid_columns[table] = {c["name"] for c in columns}

    # Build a transaction-local map only. It is discarded at commit and is never
    # available to application code as a UUID compatibility layer.
    op.execute("CREATE TEMP TABLE custom_id_map (kind text, old_id text, new_id varchar(8), PRIMARY KEY(kind, old_id)) ON COMMIT DROP")
    for table, kind in ENTITY_TABLES.items():
        if table not in uuid_columns:
            continue
        pk = list(inspector.get_pk_constraint(table)["constrained_columns"])
        if "id" not in uuid_columns[table]:
            raise RuntimeError(f"Unexpected primary key layout for {table}: {pk}")
        values = bind.execute(sa.text(f'SELECT id::text FROM "{table}"')).scalars().all()
        used = set()
        for old_id in values:
            new_id = generate_id()
            while new_id in used:
                new_id = generate_id()
            used.add(new_id)
            bind.execute(
                sa.text("INSERT INTO custom_id_map(kind, old_id, new_id) VALUES (:kind,:old,:new)"),
                {"kind": kind, "old": old_id, "new": new_id},
            )

    # Add the public user handle while preserving Telegram's numeric ID as the
    # private application key. Random collisions are retried before the unique index.
    op.add_column("users", sa.Column("public_id", sa.String(8), nullable=True))
    seen_users = set()
    users = bind.execute(sa.text("SELECT id FROM users ORDER BY id")).scalars().all()
    for user_id in users:
        public_id = generate_id()
        while public_id in seen_users:
            public_id = generate_id()
        seen_users.add(public_id)
        bind.execute(sa.text("UPDATE users SET public_id=:id WHERE id=:user_id"), {"id": public_id, "user_id": user_id})
    op.alter_column("users", "public_id", nullable=False)
    op.create_unique_constraint("uq_users_public_id", "users", ["public_id"])

    dropped_fks = []
    dropped_pks = []
    affected = set(uuid_columns)
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if table in affected or fk.get("referred_table") in affected:
                dropped_fks.append((table, fk))
                if fk.get("name"):
                    op.drop_constraint(fk["name"], table, type_="foreignkey")
    for table in affected:
        pk = inspector.get_pk_constraint(table)
        if pk.get("name") and any(col in uuid_columns[table] for col in pk["constrained_columns"]):
            dropped_pks.append((table, pk))
            op.drop_constraint(pk["name"], table, type_="primary")

    if "model_jobs" in uuid_columns:
        # Model workers are drained for cutover; their UUID-named transient files
        # are discarded after backup instead of retaining stale path references.
        op.execute("UPDATE model_jobs SET input_path = NULL, output_path = NULL, input_text = NULL")

    op.execute("""
        CREATE FUNCTION pg_temp.custom_id(kind text, old_id text) RETURNS varchar(8)
        LANGUAGE sql STABLE AS $$
          SELECT new_id FROM custom_id_map WHERE custom_id_map.kind = $1 AND custom_id_map.old_id = $2
        $$
    """)
    for table, columns in uuid_columns.items():
        for column in columns:
            kind = COLUMN_KINDS.get((table, column))
            if kind is None:
                if column == "claim_token":
                    op.execute(sa.text(f'UPDATE "{table}" SET "{column}" = NULL'))
                    op.alter_column(table, column, type_=sa.String(24), postgresql_using=f'NULL::varchar(24)')
                    continue
                raise RuntimeError(f"No custom ID mapping is defined for {table}.{column}")
            op.alter_column(
                table, column, type_=sa.String(8),
                postgresql_using=f'pg_temp.custom_id(\'{kind}\', "{column}"::text)',
            )

    for table, pk in dropped_pks:
        op.create_primary_key(pk["name"], table, pk["constrained_columns"])
    for table, fk in dropped_fks:
        if not fk.get("name"):
            raise RuntimeError(f"Unnamed foreign key on {table} needs manual migration")
        op.create_foreign_key(
            fk["name"], table, fk["referred_table"], fk["constrained_columns"],
            fk["referred_columns"], ondelete=fk.get("options", {}).get("ondelete"),
            onupdate=fk.get("options", {}).get("onupdate"),
        )


def downgrade():
    raise RuntimeError("Custom IDs intentionally have no UUID backward-compatibility downgrade")
