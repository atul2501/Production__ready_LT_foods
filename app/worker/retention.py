import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import models
from app.db.base import SessionLocal
from app.logging_conf import configure_logging, get_logger
from app.storage.local_fs import LocalFileStorage

logger = get_logger(__name__)
storage = LocalFileStorage(settings.storage_dir)

_TERMINAL_STATUSES = (
    models.JobStatusEnum.success,
    models.JobStatusEnum.needs_review,
    models.JobStatusEnum.failed,
)


def cleanup_expired_jobs() -> int:
    """Deletes completed jobs (+ their extraction/audit records via the cascade already
    defined on Job's relationships, + the stored PDF) older than settings.retention_days.
    Batched, not one giant transaction - safe to run against a table being actively
    written to. File is deleted before the DB row, and storage.delete() is idempotent, so
    a crash mid-batch just retries the same rows harmlessly on the next daily run rather
    than leaking orphaned files or losing a DB row for a file that's already gone.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_days)
    total = 0

    while True:
        session = SessionLocal()
        try:
            batch = session.scalars(
                select(models.Job)
                .where(models.Job.status.in_(_TERMINAL_STATUSES))
                .where(models.Job.created_at < cutoff)
                .order_by(models.Job.created_at.asc())
                .limit(settings.retention_batch_size)
            ).all()
            if not batch:
                break

            for job in batch:
                try:
                    storage.delete(job.storage_key)
                except Exception:  # noqa: BLE001
                    logger.exception("retention_file_delete_failed", job_id=job.id)
                    continue  # leave this row for the next cycle rather than losing the file reference
                session.delete(job)

            session.commit()
            total += len(batch)
            logger.info("retention_batch_deleted", count=len(batch))
        finally:
            session.close()
        time.sleep(0.2)

    logger.info("retention_cleanup_complete", total_deleted=total, retention_days=settings.retention_days)
    return total


if __name__ == "__main__":
    configure_logging()
    cleanup_expired_jobs()
