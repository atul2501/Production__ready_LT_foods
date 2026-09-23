"""email_ingest_state table: per-folder IMAP UID watermark (only new unread mail)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_ingest_state",
        sa.Column("folder", sa.String(), primary_key=True),
        sa.Column("uid_validity", sa.BigInteger(), nullable=False),
        sa.Column("last_uid", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("email_ingest_state")
