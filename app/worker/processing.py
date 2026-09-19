from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import ExtractionError
from app.db import models
from app.logging_conf import get_logger
from app.metrics import JOBS_COMPLETED, PIPELINE_DURATION_SECONDS
from app.pipeline.run import run_pipeline
from app.storage.local_fs import LocalFileStorage

logger = get_logger(__name__)
storage = LocalFileStorage(settings.storage_dir)


def _record_audit_event(session: Session, job_id: str, event_type: str, metadata: dict | None = None) -> None:
    session.add(models.AuditEvent(job_id=job_id, event_type=event_type, event_metadata=metadata or {}))


def _backoff_seconds(retry_count: int) -> int:
    delay = settings.worker_retry_base_delay_seconds * (2 ** (retry_count - 1))
    return min(delay, settings.worker_retry_max_delay_seconds)


def _fenced_update(session: Session, job_id: str, lease_token: str, **values) -> bool:
    """Writes back to a claimed job only if we still hold its current lease. If the
    reaper already reclaimed it (a new lease_token was issued on the next claim), this
    matches zero rows and the stale write is discarded instead of racing/overwriting the
    newer attempt's result - this is what actually prevents double-processing corruption,
    not the reap threshold being conservative."""
    result = session.execute(
        update(models.Job).where(models.Job.id == job_id, models.Job.lease_token == lease_token).values(**values)
    )
    return result.rowcount > 0


def process_job(session: Session, job_id: str, lease_token: str) -> None:
    """Processes one claimed job. Mirrors the old Celery task body, minus anything Celery
    itself used to provide (dispatch, retry, dead-lettering) - all replaced by the
    claim/lease/retry-column mechanism in claim.py + the caller in runner.py."""
    log = logger.bind(job_id=job_id)
    log.info("job_claimed", lease_token=lease_token)

    job = session.get(models.Job, job_id)
    if job is None:
        log.error("job_not_found")
        return

    # Defense in depth: claim_next_job only ever claims status='queued' rows, so this
    # should never trigger, but a stale reprocessing of an already-terminal job is exactly
    # the failure class that bit us with Celery/Redis - cheap to guard here too.
    if job.status in (models.JobStatusEnum.success, models.JobStatusEnum.needs_review, models.JobStatusEnum.failed):
        log.warning("duplicate_processing_skipped", job_status=job.status.value)
        return

    _record_audit_event(session, job_id, "processing_started")
    session.commit()

    try:
        pdf_bytes = storage.read(job.storage_key)
        log.info("pdf_loaded_from_storage", storage_key=job.storage_key, size_bytes=len(pdf_bytes))

        try:
            result = run_pipeline(pdf_bytes, job_id)
        except ExtractionError as exc:
            written = _fenced_update(
                session, job_id, lease_token,
                status=models.JobStatusEnum.failed,
                failure_reason=str(exc),
            )
            if not written:
                session.rollback()
                log.warning("stale_write_discarded", stage="extraction_failed")
                return
            _record_audit_event(session, job_id, "failed", {"error": str(exc)})
            session.commit()
            JOBS_COMPLETED.labels(status="failed").inc()
            log.error("extraction_failed", error=str(exc))
            return

        extraction = models.InvoiceExtraction(
            job_id=job_id,
            extracted_json=result["extraction"].model_dump(),
            flags=result["flags"],
            status=models.JobStatusEnum(result["status"]),
            model_name=result["model_name"],
            prompt_version=result["prompt_version"],
            extraction_source=result["extraction_source"],
            source_text_hash=result["source_text_hash"],
            processing_time_ms=result["processing_time_ms"],
        )

        written = _fenced_update(session, job_id, lease_token, status=models.JobStatusEnum(result["status"]))
        if not written:
            session.rollback()
            log.warning("stale_write_discarded", stage="extraction_complete")
            return

        session.add(extraction)
        _record_audit_event(
            session, job_id, "extraction_complete",
            {"status": result["status"], "processing_time_ms": result["processing_time_ms"]},
        )
        session.commit()
        JOBS_COMPLETED.labels(status=result["status"]).inc()
        PIPELINE_DURATION_SECONDS.observe(result["processing_time_ms"] / 1000)
        log.info("extraction_persisted", extraction_id=extraction.id, status=result["status"])
        log.info("job_complete", status=result["status"])

    except Exception as exc:  # noqa: BLE001 - unexpected failures get automatic retry-with-backoff below
        session.rollback()
        log.exception("unexpected_pipeline_error")

        job = session.get(models.Job, job_id)
        if job is None or job.lease_token != lease_token:
            log.warning("stale_write_discarded", stage="unexpected_error")
            return

        if job.retry_count >= settings.worker_max_retries:
            job.status = models.JobStatusEnum.failed
            job.failure_reason = f"max retries exceeded: {exc}"
            _record_audit_event(session, job_id, "failed", {"error": str(exc)})
            JOBS_COMPLETED.labels(status="failed").inc()
            log.error("max_retries_exceeded", error=str(exc))
        else:
            job.retry_count += 1
            job.status = models.JobStatusEnum.queued
            job.failure_reason = str(exc)
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(job.retry_count))
            job.lease_token = None
            _record_audit_event(
                session, job_id, "auto_retry_scheduled",
                {"retry_count": job.retry_count, "next_attempt_at": job.next_attempt_at.isoformat()},
            )
            log.warning("auto_retry_scheduled", retry_count=job.retry_count, next_attempt_at=str(job.next_attempt_at))
        session.commit()
    finally:
        log.info("job_finished")
