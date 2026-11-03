"""Persistent registration and multiple outgoing actions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_access"
down_revision = "0001_foundation"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        *[
            sa.Column(name, sa.String())
            for name in ("first_name", "last_name", "username", "language_code")
        ],
        sa.Column("registered_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("private", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','revoked')", name="user_status"
        ),
    )
    op.create_table(
        "access_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_status", sa.String(16)),
        sa.Column("new_status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.drop_constraint("telegram_deliveries_update_id_key", "telegram_deliveries", type_="unique")
    op.add_column(
        "telegram_deliveries",
        sa.Column("action_key", sa.String(100), nullable=False, server_default="reply"),
    )
    op.add_column(
        "telegram_deliveries",
        sa.Column("audience", sa.String(20), nullable=False, server_default="protected"),
    )
    op.create_unique_constraint(
        "uq_delivery_action", "telegram_deliveries", ["update_id", "action_key"]
    )


def downgrade():
    # A populated multi-action outbox cannot safely collapse to one action per update.
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM telegram_deliveries GROUP BY update_id HAVING count(*) > 1)"
        )
    ):
        raise RuntimeError("Drain and archive the multi-action outbox before downgrading")
    op.drop_constraint("uq_delivery_action", "telegram_deliveries", type_="unique")
    op.drop_column("telegram_deliveries", "audience")
    op.drop_column("telegram_deliveries", "action_key")
    op.create_unique_constraint(
        "telegram_deliveries_update_id_key", "telegram_deliveries", ["update_id"]
    )
    op.drop_table("access_changes")
    op.drop_table("users")
