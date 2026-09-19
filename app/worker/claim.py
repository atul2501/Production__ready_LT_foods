import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models
from app.logging_conf import get_logger

logger = get_logger(__name__)

_CLAIM_SQL = text(
    """
    UPDATE jobs
    SET status = 'processing', updated_at = now(), lease_token = :lease_token
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now())
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def claim_next_job(session: Session) -> tuple[str, str] | None:
    """Atomically claims one queued (and due) job. Race-safe across any number of
    threads/processes: Postgres's row lock manager arbitrates it, not application code -
    SKIP LOCKED means a concurrent claimant never blocks on this one, it just locks a
    different row. Single round-trip statement, so there's no window between "pick a row"
    and "mark it taken" for another claimant to interleave into.

    Returns (job_id, lease_token) or None if nothing is claimable right now.
    """
    lease_token = str(uuid.uuid4())
    row = session.execute(_CLAIM_SQL, {"lease_token": lease_token}).first()
    session.commit()
    if row is None:
        return None
    # Raw SQL returns the uuid column as a native uuid.UUID via psycopg2; the rest of the
    # codebase treats job ids as plain str (Job.id uses UUID(as_uuid=False)) - normalize
    # here so callers never have to think about the difference.
    return str(row[0]), lease_token


def reap_stale_jobs(session: Session) -> int:
    """Reclaims jobs stuck in 'processing' longer than the stale threshold - covers a
    worker crash/restart mid-job (the direct replacement for the Redis visibility_timeout
    fix from the Celery/Redis era). Correctness against falsely reclaiming a job that's
    actually still running (just slow) comes from lease_token fencing on the final write
    in processing.py, not from this threshold being exactly right - so this can stay a
    simple flat timeout rather than needing a per-job heartbeat mechanism.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.worker_stale_threshold_seconds)
    result = session.execute(
        update(models.Job)
        .where(models.Job.status == models.JobStatusEnum.processing)
        .where(models.Job.updated_at < cutoff)
        .values(
            status=models.JobStatusEnum.queued,
            retry_count=models.Job.retry_count + 1,
            failure_reason="reclaimed: stale processing (worker likely crashed or restarted)",
            next_attempt_at=None,
            lease_token=None,
        )
        .returning(models.Job.id)
    )
    reclaimed_ids = [row[0] for row in result]
    session.commit()
    if reclaimed_ids:
        logger.warning("stale_jobs_reclaimed", job_ids=reclaimed_ids, count=len(reclaimed_ids))
    return len(reclaimed_ids)
