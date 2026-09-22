"""processed_emails table for IMAP email-ingest dedup

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_emails",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_processed_emails_message_id", "processed_emails", ["message_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_processed_emails_message_id", table_name="processed_emails")
    op.drop_table("processed_emails")
