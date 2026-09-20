from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.invoice_schema import AdditionalField, InvoiceHeader, LineItem


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ValidationFlag(BaseModel):
    field: str
    reason: str
    severity: str  # "warning" | "critical"
    detail: Optional[str] = None


class ExtractionMetadata(BaseModel):
    # model_name isn't one of pydantic's own model_* methods, just our field name - silence
    # pydantic's protected-namespace warning rather than renaming a field the API returns.
    model_config = ConfigDict(protected_namespaces=())

    flags: list[ValidationFlag] = []
    model_name: Optional[str] = None
    prompt_version: Optional[str] = None
    extraction_source: Optional[str] = None
    processing_time_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    failure_reason: Optional[str] = None


class JobResultResponse(BaseModel):
    job_id: str
    status: JobStatus
    invoice_header: InvoiceHeader
    line_items: list[LineItem]
    additional_fields: list[AdditionalField]
    metadata: ExtractionMetadata
