from app.pipeline.grounding import parse_date
from app.schemas.envelope import (
    OUTPUT_DATE_FORMAT,
    EmailInfo,
    ExtractionMetadata,
    InvoiceResult,
    ResultStatus,
    ValidationFlag,
)


def _format_date(value: str | None) -> str | None:
    """DD.MM.YYYY for the API. A value that isn't a recognisable date is passed through
    unchanged rather than dropped or guessed at."""
    parsed = parse_date(value) if value else None
    return parsed.strftime(OUTPUT_DATE_FORMAT) if parsed else value


def build_result(result_id: str, pipeline_result: dict, filename: str | None, email: EmailInfo | None) -> InvoiceResult:
    """Maps run_pipeline()'s return value to the JSON shape the API hands out."""
    extraction = pipeline_result["extraction"]
    # Reformatted here, after grounding, so grounding still checks the LLM's raw value.
    header = extraction.invoice_header.model_copy(
        update={
            "invoice_date": _format_date(extraction.invoice_header.invoice_date),
            "due_date": _format_date(extraction.invoice_header.due_date),
        }
    )
    return InvoiceResult(
        id=result_id,
        status=ResultStatus(pipeline_result["status"]),
        filename=filename,
        email=email,
        invoice_header=header,
        line_items=extraction.line_items,
        additional_fields=extraction.additional_fields,
        metadata=ExtractionMetadata(
            flags=[ValidationFlag(**flag) for flag in pipeline_result["flags"]],
            model_name=pipeline_result["model_name"],
            prompt_version=pipeline_result["prompt_version"],
            extraction_source=pipeline_result["extraction_source"],
            processing_time_ms=pipeline_result["processing_time_ms"],
            completed_at=pipeline_result["completed_at"],
        ),
    )


def build_failure(result_id: str, error: str, filename: str | None, email: EmailInfo | None) -> InvoiceResult:
    return InvoiceResult(
        id=result_id,
        status=ResultStatus.FAILED,
        filename=filename,
        email=email,
        error=error,
    )
