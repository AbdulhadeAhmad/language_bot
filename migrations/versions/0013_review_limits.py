"""Add user-specific daily new-card and session limits."""

import sqlalchemy as sa
from alembic import op

revision = "0013_review_limits"
down_revision = "0012_review_content_revision"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("new_cards_per_day", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("users", sa.Column("review_session_limit", sa.Integer(), nullable=False, server_default="20"))
    op.create_check_constraint("user_new_cards_per_day", "users", "new_cards_per_day BETWEEN 0 AND 100")
    op.create_check_constraint("user_review_session_limit", "users", "review_session_limit BETWEEN 1 AND 100")


def downgrade():
    op.drop_constraint("user_review_session_limit", "users", type_="check")
    op.drop_constraint("user_new_cards_per_day", "users", type_="check")
    op.drop_column("users", "review_session_limit")
    op.drop_column("users", "new_cards_per_day")
