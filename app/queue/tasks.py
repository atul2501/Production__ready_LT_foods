from app.config import settings
from app.core.exceptions import ExtractionError
from app.db import models
from app.db.base import SessionLocal
from app.logging_conf import get_logger
from app.pipeline.run import run_pipeline
from app.queue.celery_app import celery_app
from app.storage.local_fs import LocalFileStorage

logger = get_logger(__name__)
storage = LocalFileStorage(settings.storage_dir)


def _record_audit_event(session, job_id: str, event_type: str, metadata: dict | None = None) -> None:
    session.add(models.AuditEvent(job_id=job_id, event_type=event_type, event_metadata=metadata or {}))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, name="process_invoice")
def process_invoice(self, job_id: str) -> None:
    log = logger.bind(job_id=job_id)
    log.info("task_received", celery_task_id=self.request.id, retries=self.request.retries)
    session = SessionLocal()
    try:
        job = session.get(models.Job, job_id)
        if job is None:
            log.error("job_not_found")
            return

        # Redis's at-least-once delivery (acks_late=True) can redeliver the same message
        # after a long delay if it was in-flight during a worker restart - observed directly:
        # a message from before the visibility_timeout fix (see celery_app.py) resurfaced
        # ~30-40 min later and re-ran a job that had already completed successfully. A job
        # only reaches queued/processing through a fresh upload or a deliberate /retry call
        # (both explicitly set status to queued first) - if we're starting from a *terminal*
        # status instead, this is a stale duplicate delivery of an already-resolved job, not
        # new work. Skip re-running the expensive pipeline rather than silently overwriting
        # a completed result and wasting an LLM call.
        if job.status in (
            models.JobStatusEnum.success,
            models.JobStatusEnum.needs_review,
            models.JobStatusEnum.failed,
        ):
            log.warning("duplicate_delivery_skipped", job_status=job.status.value)
            return

        job.status = models.JobStatusEnum.processing
        _record_audit_event(session, job_id, "processing_started")
        session.commit()
        log.info("job_status_updated", status="processing")

        pdf_bytes = storage.read(job.storage_key)
        log.info("pdf_loaded_from_storage", storage_key=job.storage_key, size_bytes=len(pdf_bytes))

        try:
            result = run_pipeline(pdf_bytes, job_id)
        except ExtractionError as exc:
            job.status = models.JobStatusEnum.failed
            job.failure_reason = str(exc)
            _record_audit_event(session, job_id, "failed", {"error": str(exc)})
            session.commit()
            log.error("extraction_failed", error=str(exc))
            log.info("job_status_updated", status="failed")
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
        session.add(extraction)
        job.status = models.JobStatusEnum(result["status"])
        _record_audit_event(
            session,
            job_id,
            "extraction_complete",
            {"status": result["status"], "processing_time_ms": result["processing_time_ms"]},
        )
        session.commit()
        log.info("extraction_persisted", extraction_id=extraction.id, status=result["status"])
        log.info("job_complete", status=result["status"])

    except Exception as exc:  # noqa: BLE001 - transient failures retried, then dead-lettered below
        session.rollback()
        log.exception("unexpected_pipeline_error")
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            log.error("max_retries_exceeded", error=str(exc))
            job = session.get(models.Job, job_id)
            if job is not None:
                job.status = models.JobStatusEnum.failed
                job.failure_reason = f"max retries exceeded: {exc}"
                _record_audit_event(session, job_id, "failed", {"error": str(exc)})
                session.commit()
                log.info("job_status_updated", status="failed")
    finally:
        session.close()
        log.info("task_finished")
