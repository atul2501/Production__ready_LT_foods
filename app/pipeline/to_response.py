from app.schemas.envelope import (
    EmailInfo,
    ExtractionMetadata,
    InvoiceResult,
    ResultStatus,
    ValidationFlag,
)


def build_result(result_id: str, pipeline_result: dict, filename: str | None, email: EmailInfo | None) -> InvoiceResult:
    """Maps run_pipeline()'s return value to the JSON shape the API hands out."""
    extraction = pipeline_result["extraction"]
    return InvoiceResult(
        id=result_id,
        status=ResultStatus(pipeline_result["status"]),
        filename=filename,
        email=email,
        invoice_header=extraction.invoice_header,
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
