"""index jobs.original_filename for lookup-by-PDF-name

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23

"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_original_filename", "jobs", ["original_filename"])


def downgrade() -> None:
    op.drop_index("ix_jobs_original_filename", table_name="jobs")
