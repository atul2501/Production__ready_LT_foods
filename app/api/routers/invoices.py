import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader

from app.config import settings
from app.logging_conf import get_logger
from app.pipeline.run import run_pipeline
from app.pipeline.to_response import build_failure, build_result
from app.schemas.envelope import InvoiceResult, JobAccepted
from app.storage import results_store

logger = get_logger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Uploaded PDFs are extracted here, in the background, so POST /invoices answers at once.
_upload_executor = ThreadPoolExecutor(max_workers=settings.upload_max_concurrent, thread_name_prefix="upload")


def require_api_key(request: Request, provided: str | None = Security(_api_key_header)) -> None:
    if not settings.api_key:
        logger.error("api_key_not_configured")
        raise HTTPException(status_code=503, detail="API_KEY not configured on the server")
    if not provided or not secrets.compare_digest(provided.encode(), settings.api_key.encode()):
        logger.warning("api_key_rejected", client=request.client.host if request.client else None, path=request.url.path)
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


router = APIRouter(prefix="/api/v1", tags=["invoices"], dependencies=[Depends(require_api_key)])


@router.get("/invoices/new", response_model=list[InvoiceResult])
def get_new_invoices() -> list[dict]:
    """Every invoice extracted from email since the last call, oldest first. Each result is
    returned exactly once: calling again straight away returns []. Results are extracted in
    the background by the email poller (email_ingest_main.py), so this responds instantly."""
    return results_store.claim_pending()


@router.get("/invoices/{result_id}/pdf", response_class=FileResponse)
def get_invoice_pdf(result_id: str) -> FileResponse:
    """The original PDF of an invoice - an uploaded job or an email result (its pdf_url),
    shown inline in the browser/Postman. Kept for PDF_RETENTION_DAYS (default 30)."""
    path = results_store.pdf_path(result_id)
    if path is None:
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")


@router.get(
    "/invoices/{result_id}",
    response_model=InvoiceResult,
    responses={202: {"model": JobAccepted, "description": "Still extracting - call again shortly"}},
)
def get_invoice(result_id: str):
    """The JSON for one id: the job id returned by POST /invoices, or the id of an email
    result. 202 with status "processing" while an upload is still being extracted, 200 with
    the full result once it is done (status success / needs_review / failed)."""
    logger.info("get_invoice_request", result_id=result_id)
    logger.info("hello")
    logger.info("welcome")
    result =results_store.get_result(result_id)
    if result is not None:
        logger.info("get_invoice_response", result_id=result_id, status_code=200, response=result)
        return result

    started_at = results_store.job_started_at(result_id)
    if started_at is None:
        logger.warning("get_invoice_response", result_id=result_id, status_code=404, response="no invoice or job with this id")
        raise HTTPException(status_code=404, detail="no invoice or job with this id")
    if time.time() - started_at > settings.upload_job_timeout_seconds:
        # The API restarted mid-extraction, so this job will never finish.
        failure = build_failure(result_id, "extraction was interrupted - upload the PDF again", None, None)
        failure.pdf_url = results_store.pdf_url(result_id)
        logger.info("get_invoice_response", result_id=result_id, status_code=200, response=failure.model_dump(mode="json"))
        return failure
    content = _job_accepted(result_id).model_dump(mode="json")
    logger.info("get_invoice_response", result_id=result_id, status_code=202, response=content)
    logger.info(f"->{content}")
    return JSONResponse(status_code=202, content=content)


@router.post("/invoices", response_model=JobAccepted, status_code=202)
async def extract_invoice(request: Request) -> JobAccepted:
    """Uploads one PDF and returns its job id at once; the PDF is extracted in the
    background. Poll GET /invoices/{id} (the result_url) for the JSON and open
    GET /invoices/{id}/pdf (the pdf_url) for the PDF. Accepts either a raw binary PDF
    request body (Postman "binary" body mode, Content-Type: application/pdf or
    application/octet-stream) or a multipart/form-data upload with a "file" field
    (browsers, Swagger UI's file picker)."""
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

    job_id = str(uuid.uuid4())
    results_store.save_pdf(job_id, content)
    results_store.start_job(job_id)
    _upload_executor.submit(_run_upload_job, job_id, content, filename)
    accepted = _job_accepted(job_id)
    logger.info("upload_job_created", job_id=job_id, filename=filename, response=accepted.model_dump(mode="json"))
    return accepted


def _job_accepted(job_id: str) -> JobAccepted:
    return JobAccepted(
        id=job_id,
        status="processing",
        result_url=f"/api/v1/invoices/{job_id}",
        pdf_url=results_store.pdf_url(job_id),
    )


def _run_upload_job(job_id: str, content: bytes, filename: str | None) -> None:
    try:
        response = build_result(job_id, run_pipeline(content, job_id), filename, email=None)
    except Exception as exc:  # noqa: BLE001 - the job must always end with a result
        logger.exception("extraction_failed", result_id=job_id)
        response = build_failure(job_id, f"{type(exc).__name__}: {exc}", filename, email=None)
    response.pdf_url = results_store.pdf_url(job_id)
    data = response.model_dump(mode="json")
    results_store.finish_job(job_id, data)
    logger.info("upload_result", result_id=job_id, result=data)
