"""Add opt-in local-time review reminders."""

import sqlalchemy as sa
from alembic import op

revision = "0014_daily_reminders"
down_revision = "0013_review_limits"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("reminder_enabled", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column(
        "users", sa.Column("reminder_timezone", sa.String(length=64), nullable=False, server_default="UTC")
    )
    op.add_column(
        "users", sa.Column("reminder_time", sa.Time(), nullable=False, server_default="20:00:00")
    )
    op.add_column("users", sa.Column("last_reminder_local_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("users", "last_reminder_local_date")
    op.drop_column("users", "reminder_time")
    op.drop_column("users", "reminder_timezone")
    op.drop_column("users", "reminder_enabled")
