"""Preserve old grade meanings while storing the five-button review scale."""

from alembic import op

revision = "0018_five_point_review_ratings"
down_revision = "0017_review_batch_questions"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("review_attempt_rating", "review_attempts", type_="check")
    op.execute(
        """
        UPDATE review_attempts
        SET rating = CASE rating WHEN 3 THEN 4 WHEN 4 THEN 5 ELSE rating END
        """
    )
    op.create_check_constraint(
        "review_attempt_rating", "review_attempts", "rating BETWEEN 1 AND 5"
    )

    op.drop_constraint(
        "review_batch_question_rating", "review_batch_questions", type_="check"
    )
    op.execute(
        """
        UPDATE review_batch_questions
        SET rating = CASE rating WHEN 3 THEN 4 WHEN 4 THEN 5 ELSE rating END
        WHERE rating IS NOT NULL
        """
    )
    op.create_check_constraint(
        "review_batch_question_rating",
        "review_batch_questions",
        "rating IS NULL OR rating BETWEEN 1 AND 5",
    )


def downgrade():
    op.drop_constraint("review_attempt_rating", "review_attempts", type_="check")
    op.execute(
        """
        UPDATE review_attempts
        SET rating = CASE WHEN rating IN (3, 4) THEN 3 WHEN rating = 5 THEN 4 ELSE rating END
        """
    )
    op.create_check_constraint(
        "review_attempt_rating", "review_attempts", "rating BETWEEN 1 AND 4"
    )

    op.drop_constraint(
        "review_batch_question_rating", "review_batch_questions", type_="check"
    )
    op.execute(
        """
        UPDATE review_batch_questions
        SET rating = CASE WHEN rating IN (3, 4) THEN 3 WHEN rating = 5 THEN 4 ELSE rating END
        WHERE rating IS NOT NULL
        """
    )
    op.create_check_constraint(
        "review_batch_question_rating",
        "review_batch_questions",
        "rating IS NULL OR rating BETWEEN 1 AND 4",
    )
