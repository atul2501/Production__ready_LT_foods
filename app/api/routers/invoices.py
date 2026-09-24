import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.core.exceptions import ExtractionError
from app.logging_conf import get_logger
from app.pipeline.run import run_pipeline
from app.pipeline.to_response import build_result
from app.schemas.envelope import InvoiceResult
from app.storage import results_store

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["invoices"])


@router.get("/invoices/new", response_model=list[InvoiceResult])
def get_new_invoices() -> list[dict]:
    """Every invoice extracted from email since the last call, oldest first. Each result is
    returned exactly once: calling again straight away returns []. Results are extracted in
    the background by the email poller (email_ingest_main.py), so this responds instantly."""
    return results_store.claim_pending()


@router.post("/invoices", response_model=InvoiceResult)
async def extract_invoice(request: Request) -> InvoiceResult:
    """Extracts one PDF directly and returns its JSON - for testing; nothing is stored.
    Accepts either a raw binary PDF request body (Postman "binary" body mode,
    Content-Type: application/pdf or application/octet-stream) or a multipart/form-data
    upload with a "file" field (browsers, Swagger UI's file picker). Takes as long as the
    extraction does (up to a few minutes for a scanned PDF)."""
    content_type = request.headers.get("content-type", "")
    filename: str | None = None

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            logger.warning("upload_rejected", reason="missing_file_field")
            raise HTTPException(status_code=400, detail="multipart upload must include a 'file' field")
        content = await upload.read()
        filename = getattr(upload, "filename", None)
    else:
        content = await request.body()

    logger.info("upload_received", filename=filename, content_type=content_type, size_bytes=len(content))

    if not content:
        logger.warning("upload_rejected", filename=filename, reason="empty_file")
        raise HTTPException(status_code=400, detail="empty file")
    if not content.startswith(b"%PDF-"):
        logger.warning("upload_rejected", filename=filename, reason="not_a_pdf")
        raise HTTPException(status_code=400, detail="file is not a valid PDF")

    result_id = str(uuid.uuid4())
    try:
        result = await run_in_threadpool(run_pipeline, content, result_id)
    except ExtractionError as exc:
        logger.error("extraction_failed", result_id=result_id, error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return build_result(result_id, result, filename, email=None)
