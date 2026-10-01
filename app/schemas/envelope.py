from datetime import datetime
from enum import Enum
from typing import Annotated, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer
from app.schemas.invoice_schema import AdditionalField, InvoiceHeader, LineItem

OUTPUT_DATE_FORMAT = "%d.%m.%Y"


def _parse_output_date(value: object) -> object:
    # Stored results are read back through these models (GET /invoices/...), so a
    # DD.MM.YYYY string written by the serializer below must validate again. Anything else
    # (a datetime, or an ISO string in results stored before this format) is left to pydantic.
    if isinstance(value, str):
        try:
            return datetime.strptime(value, OUTPUT_DATE_FORMAT)
        except ValueError:
            pass
    return value


# A timestamp the API hands out as a date only, DD.MM.YYYY.
OutputDate = Annotated[
    datetime,
    BeforeValidator(_parse_output_date),
    PlainSerializer(lambda value: value.strftime(OUTPUT_DATE_FORMAT), return_type=str, when_used="json"),
]


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
    completed_at: Optional[OutputDate] = None


class EmailInfo(BaseModel):
    message_id: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    received_at: Optional[OutputDate] = None


class InvoiceResult(BaseModel):
    """One extracted PDF. Written to STORAGE_DIR/pending/ by the email poller and returned
    as-is by GET /api/v1/invoices/new. status "failed" means the PDF could not be extracted
    after every attempt - invoice_header is null and error says why."""

    id: str
    status: ResultStatus
    filename: Optional[str] = None
    # Path of the original PDF on this API (GET, same X-API-Key).
    pdf_url: Optional[str] = None
    email: Optional[EmailInfo] = None
    invoice_header: Optional[InvoiceHeader] = None
    line_items: list[LineItem] = []
    additional_fields: list[AdditionalField] = []
    metadata: ExtractionMetadata = ExtractionMetadata()
    error: Optional[str] = None


class JobAccepted(BaseModel):
    """Answer to POST /api/v1/invoices (and to GET /api/v1/invoices/{id} while the upload
    is still being extracted): fetch result_url for the JSON, pdf_url for the PDF."""

    id: str
    status: str  # "processing"
    result_url: str
    pdf_url: str
