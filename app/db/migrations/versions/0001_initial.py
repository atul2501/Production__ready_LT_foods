"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

job_status_enum = postgresql.ENUM(
    "queued", "processing", "success", "needs_review", "failed",
    name="job_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    job_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("status", job_status_enum, nullable=False, server_default="queued"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_jobs_file_hash", "jobs", ["file_hash"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "invoice_extractions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extracted_json", postgresql.JSONB(), nullable=False),
        sa.Column("flags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", job_status_enum, nullable=False),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("extraction_source", sa.String(), nullable=True),
        sa.Column("source_text_hash", sa.String(64), nullable=True),
        sa.Column("processing_time_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_invoice_extractions_job_id", "invoice_extractions", ["job_id"])
    op.create_index(
        "ix_invoice_extractions_invoice_number",
        "invoice_extractions",
        [sa.text("(extracted_json -> 'invoice_header' ->> 'invoice_number')")],
    )
    op.create_index(
        "ix_invoice_extractions_vendor_name",
        "invoice_extractions",
        [sa.text("(extracted_json -> 'invoice_header' ->> 'vendor_name')")],
    )
    op.create_index(
        "ix_invoice_extractions_po_number",
        "invoice_extractions",
        [sa.text("(extracted_json -> 'invoice_header' ->> 'po_number')")],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_job_id", "audit_events", ["job_id"])

    op.create_table(
        "validation_flags",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("extraction_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("invoice_extractions.id"), nullable=False),
        sa.Column("field_path", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_validation_flags_extraction_id", "validation_flags", ["extraction_id"])


def downgrade() -> None:
    op.drop_table("validation_flags")
    op.drop_table("audit_events")
    op.drop_table("invoice_extractions")
    op.drop_table("jobs")
    job_status_enum.drop(op.get_bind(), checkfirst=True)
