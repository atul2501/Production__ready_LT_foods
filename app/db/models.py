import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatusEnum(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    success = "success"
    needs_review = "needs_review"
    failed = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    file_hash = Column(String(64), index=True, nullable=False)
    original_filename = Column(Text, nullable=True)
    storage_key = Column(Text, nullable=False)
    status = Column(
        SAEnum(JobStatusEnum, name="job_status"),
        nullable=False,
        default=JobStatusEnum.queued,
        index=True,
    )
    failure_reason = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(Text, nullable=True)
    # Backoff gate for the automatic-retry queue: a queued row with next_attempt_at in the
    # future is invisible to the claim query's WHERE clause until that time passes -
    # Postgres itself is the timer, no external scheduler needed.
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    # Set fresh on every claim; every write a worker makes back to this job (heartbeat-free
    # design: just the final result) is conditioned on matching this token, so a stale/
    # reclaimed attempt's write is discarded instead of racing the current attempt's result.
    lease_token = Column(UUID(as_uuid=False), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    extractions = relationship(
        "InvoiceExtraction", back_populates="job", cascade="all, delete-orphan"
    )
    audit_events = relationship(
        "AuditEvent", back_populates="job", cascade="all, delete-orphan"
    )


class InvoiceExtraction(Base):
    __tablename__ = "invoice_extractions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    extracted_json = Column(JSONB, nullable=False)
    flags = Column(JSONB, nullable=False, default=list)
    status = Column(SAEnum(JobStatusEnum, name="job_status"), nullable=False)
    model_name = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    extraction_source = Column(String, nullable=True)  # "digital" | "ocr" | "mixed"
    source_text_hash = Column(String(64), nullable=True)
    processing_time_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    job = relationship("Job", back_populates="extractions")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    event_metadata = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    job = relationship("Job", back_populates="audit_events")


class ValidationFlag(Base):
    __tablename__ = "validation_flags"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    extraction_id = Column(
        UUID(as_uuid=False), ForeignKey("invoice_extractions.id"), nullable=False, index=True
    )
    field_path = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
