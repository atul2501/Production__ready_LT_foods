from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.invoice_schema import AdditionalField, InvoiceHeader, LineItem


class ResultStatus(str, Enum):
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
    completed_at: Optional[datetime] = None


class EmailInfo(BaseModel):
    message_id: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    received_at: Optional[datetime] = None


class InvoiceResult(BaseModel):
    """One extracted PDF. Written to STORAGE_DIR/pending/ by the email poller and returned
    as-is by GET /api/v1/invoices/new. status "failed" means the PDF could not be extracted
    after every attempt - invoice_header is null and error says why."""

    id: str
    status: ResultStatus
    filename: Optional[str] = None
    email: Optional[EmailInfo] = None
    invoice_header: Optional[InvoiceHeader] = None
    line_items: list[LineItem] = []
    additional_fields: list[AdditionalField] = []
    metadata: ExtractionMetadata = ExtractionMetadata()
    error: Optional[str] = None
