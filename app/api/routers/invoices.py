import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.db import models
from app.logging_conf import get_logger
from app.schemas.envelope import (
    ExtractionMetadata,
    JobCreatedResponse,
    JobResultResponse,
    JobStatus,
    JobStatusResponse,
    ValidationFlag,
)
from app.storage.local_fs import LocalFileStorage

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["invoices"])
storage = LocalFileStorage(settings.storage_dir)


def _check_backlog(db: Session) -> None:
    queued_count = db.scalar(
        select(func.count()).select_from(models.Job).where(models.Job.status == models.JobStatusEnum.queued)
    )
    if queued_count is not None and queued_count >= settings.max_queue_backlog:
        logger.warning("backlog_full", queued_count=queued_count, max_queue_backlog=settings.max_queue_backlog)
        raise HTTPException(
            status_code=503,
            detail="processing backlog full, retry later",
            headers={"Retry-After": "60"},
        )


def _persist_upload(
    db: Session, content: bytes, filename: str | None, uploaded_by: str | None = None
) -> models.Job:
    """Synchronous body of the upload handler - backlog check, SHA-256 hashing, the disk
    write, and every DB call (the sync SQLAlchemy/psycopg2 driver blocks). Run via
    run_in_threadpool below rather than directly in the async route, so one slow/large
    upload can't stall every other request the single Uvicorn event loop is serving
    concurrently. Also called directly (no HTTP hop) by app/email_ingest/ for PDFs pulled
    off an inbox, which is where uploaded_by (the sender address) comes from."""
    _check_backlog(db)

    job_id = str(uuid.uuid4())
    log = logger.bind(job_id=job_id)
    file_hash = hashlib.sha256(content).hexdigest()
    storage_key = f"{job_id}/original.pdf"
    storage.save(storage_key, content)
    log.info("pdf_saved_to_storage", storage_key=storage_key, size_bytes=len(content))

    job = models.Job(
        id=job_id,
        file_hash=file_hash,
        original_filename=filename,
        storage_key=storage_key,
        status=models.JobStatusEnum.queued,
        uploaded_by=uploaded_by,
    )
    db.add(job)
    db.add(models.AuditEvent(job_id=job_id, event_type="uploaded", event_metadata={"file_hash": file_hash}))

    # Re-uploads of an identical file create a new job rather than silently returning a
    # cached result (a "corrected" re-upload could otherwise be masked) - just flagged here
    # for SAP-side idempotency review.
    duplicate_count = db.scalar(
        select(func.count())
        .select_from(models.Job)
        .where(models.Job.file_hash == file_hash, models.Job.id != job_id)
    )
    if duplicate_count:
        log.warning("possible_duplicate_upload", file_hash=file_hash, prior_job_count=duplicate_count)
        db.add(
            models.AuditEvent(
                job_id=job_id, event_type="possible_duplicate", event_metadata={"file_hash": file_hash}
            )
        )

    db.commit()
    log.info("job_created", status="queued")
    # No dispatch call needed - the worker polls for status='queued' rows itself
    # (app/worker/claim.py); inserting the row is the entire handoff.
    return job


@router.post("/invoices", response_model=JobCreatedResponse, status_code=202)
async def upload_invoice(request: Request, db: Session = Depends(get_db)) -> JobCreatedResponse:
    """Accepts either a raw binary PDF request body (Postman "binary" body mode,
    Content-Type: application/pdf or application/octet-stream) or a multipart/form-data
    upload with a "file" field (browsers, Swagger UI's file picker) - same endpoint, same
    response either way."""
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

    job = await run_in_threadpool(_persist_upload, db, content, filename)

    return JobCreatedResponse(job_id=job.id, status=JobStatus.QUEUED, created_at=job.created_at)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str, db: Session = Depends(get_db)) -> JobStatusResponse:
    logger.debug("job_status_requested", job_id=job_id)
    job = db.get(models.Job, job_id)
    if job is None:
        logger.warning("job_status_not_found", job_id=job_id)
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatusResponse(
        job_id=job.id,
        status=JobStatus(job.status.value),
        created_at=job.created_at,
        updated_at=job.updated_at,
        failure_reason=job.failure_reason,
    )


@router.get("/jobs/{job_id}/result", response_model=JobResultResponse)
def get_job_result(job_id: str, db: Session = Depends(get_db)) -> JobResultResponse:
    logger.debug("job_result_requested", job_id=job_id)
    job = db.get(models.Job, job_id)
    if job is None:
        logger.warning("job_result_not_found", job_id=job_id)
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in (models.JobStatusEnum.queued, models.JobStatusEnum.processing):
        raise HTTPException(status_code=409, detail=f"job is still {job.status.value}")
    if job.status == models.JobStatusEnum.failed:
        raise HTTPException(status_code=422, detail=job.failure_reason or "extraction failed")

    extraction = db.scalar(
        select(models.InvoiceExtraction)
        .where(models.InvoiceExtraction.job_id == job_id)
        .order_by(models.InvoiceExtraction.version.desc())
    )
    if extraction is None:
        logger.error("job_result_missing_extraction", job_id=job_id, job_status=job.status.value)
        raise HTTPException(status_code=500, detail="job finished but no extraction record found")

    logger.info("job_result_served", job_id=job_id, status=extraction.status.value)
    data = extraction.extracted_json
    return JobResultResponse(
        job_id=job.id,
        status=JobStatus(extraction.status.value),
        invoice_header=data["invoice_header"],
        line_items=data["line_items"],
        additional_fields=data["additional_fields"],
        metadata=ExtractionMetadata(
            flags=[ValidationFlag(**flag) for flag in extraction.flags],
            model_name=extraction.model_name,
            prompt_version=extraction.prompt_version,
            extraction_source=extraction.extraction_source,
            processing_time_ms=extraction.processing_time_ms,
            created_at=job.created_at,
            completed_at=extraction.created_at,
        ),
    )


@router.get("/jobs/{job_id}/pdf")
def get_job_pdf(job_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    logger.info("job_pdf_requested", job_id=job_id)
    job = db.get(models.Job, job_id)
    if job is None:
        logger.warning("job_pdf_not_found", job_id=job_id)
        raise HTTPException(status_code=404, detail="job not found")
    if not storage.exists(job.storage_key):
        logger.error("job_pdf_missing_from_storage", job_id=job_id, storage_key=job.storage_key)
        raise HTTPException(status_code=404, detail="stored PDF not found")

    filename = job.original_filename or "invoice.pdf"
    return StreamingResponse(
        storage.stream(job.storage_key),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/jobs")
def list_jobs(
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[JobStatusResponse]:
    logger.debug("job_list_requested", status_filter=status_filter, limit=limit, offset=offset)
    query = select(models.Job).order_by(models.Job.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        query = query.where(models.Job.status == models.JobStatusEnum(status_filter.value))
    jobs = db.scalars(query).all()
    return [
        JobStatusResponse(
            job_id=job.id,
            status=JobStatus(job.status.value),
            created_at=job.created_at,
            updated_at=job.updated_at,
            failure_reason=job.failure_reason,
        )
        for job in jobs
    ]


@router.post("/jobs/{job_id}/retry", response_model=JobCreatedResponse, status_code=202)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> JobCreatedResponse:
    log = logger.bind(job_id=job_id)
    log.info("job_retry_requested")
    job = db.get(models.Job, job_id)
    if job is None:
        log.warning("job_retry_not_found")
        raise HTTPException(status_code=404, detail="job not found")

    job.status = models.JobStatusEnum.queued
    # A manual retry is a deliberate fresh attempt by a human, not one more automatic
    # attempt - reset the automatic-retry budget/backoff rather than sharing it, so a job
    # a human already retried twice doesn't have only one automatic attempt left.
    job.retry_count = 0
    job.next_attempt_at = None
    job.lease_token = None
    job.failure_reason = None
    db.add(
        models.AuditEvent(job_id=job_id, event_type="retried", event_metadata={"manual_retry": True})
    )
    db.commit()
    log.info("job_status_updated", status="queued", manual_retry=True)
    # No dispatch call needed - see upload_invoice.

    return JobCreatedResponse(job_id=job.id, status=JobStatus.QUEUED, created_at=job.created_at)
