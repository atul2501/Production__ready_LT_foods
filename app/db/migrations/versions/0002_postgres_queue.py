"""postgres-backed queue: lease_token + next_attempt_at, claim/reap indexes

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("lease_token", postgresql.UUID(as_uuid=False), nullable=True))

    # Partial indexes: only the rows the claim/reap queries actually filter on, so they
    # stay cheap as the table grows past the 30-day retention window's steady-state size.
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_reap",
        "jobs",
        ["updated_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_reap", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_column("jobs", "lease_token")
    op.drop_column("jobs", "next_attempt_at")
