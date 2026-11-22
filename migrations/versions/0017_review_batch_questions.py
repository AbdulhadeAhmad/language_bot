"""Persist the five open German-to-English questions in a review batch."""

import sqlalchemy as sa
from alembic import op

revision = "0017_review_batch_questions"
down_revision = "0016_custom_ids"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "review_batch_questions",
        sa.Column("session_id", sa.String(8), nullable=False),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("sentence_id", sa.String(8), nullable=False),
        sa.Column("sentence_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','graded','cancelled')", name="review_batch_question_state"
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 4", name="review_batch_question_rating"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["review_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sentence_id"], ["sentences.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id", "question_number"),
    )
    op.create_index(
        "ix_review_batch_questions_pending",
        "review_batch_questions",
        ["session_id", "state", "question_number"],
    )
    # Existing single-question German→English sessions remain gradeable after upgrade.
    op.execute(
        """
        INSERT INTO review_batch_questions
            (session_id, question_number, sentence_id, sentence_revision, state)
        SELECT id, question_number, current_sentence_id, question_revision, 'pending'
        FROM review_sessions
        WHERE mode = 'de_to_en' AND state = 'active'
          AND current_sentence_id IS NOT NULL AND question_revision IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade():
    op.drop_index("ix_review_batch_questions_pending", table_name="review_batch_questions")
    op.drop_table("review_batch_questions")
