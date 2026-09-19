"""SKIP LOCKED / reaper correctness against a real Postgres - not meaningfully mockable,
these are inherently SQL-semantics tests. Requires a live database matching DATABASE_URL.

    RUN_INTEGRATION=1 pytest tests/integration/test_worker_queue.py
"""
import hashlib
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="requires a live Postgres matching DATABASE_URL; set RUN_INTEGRATION=1 to run",
)


def _make_queued_job(session, models) -> str:
    job_id = str(uuid.uuid4())
    session.add(
        models.Job(
            id=job_id,
            file_hash=hashlib.sha256(job_id.encode()).hexdigest(),
            storage_key=f"{job_id}/original.pdf",
            status=models.JobStatusEnum.queued,
        )
    )
    session.commit()
    return job_id


def test_claim_next_job_is_race_safe_across_threads():
    from app.db import models
    from app.db.base import SessionLocal
    from app.worker.claim import claim_next_job

    session = SessionLocal()
    job_ids = [_make_queued_job(session, models) for _ in range(6)]
    session.close()

    claimed: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        s = SessionLocal()
        try:
            while True:
                result = claim_next_job(s)
                if result is None:
                    break
                job_id, _lease_token = result
                with lock:
                    claimed.append(job_id)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every job we created was claimed exactly once across all threads - no duplicates,
    # none skipped. Not an exact-set comparison against `claimed`: other tests in the same
    # run (e.g. test_api_upload_flow.py) can leave their own queued rows behind with no
    # worker consuming them, and claim_next_job correctly claims those too - that's
    # expected, not a race-safety violation, so we only assert about our own job_ids.
    claimed_counts = {job_id: claimed.count(job_id) for job_id in job_ids}
    assert all(count == 1 for count in claimed_counts.values()), claimed_counts
    assert set(job_ids).issubset(claimed)

    session = SessionLocal()
    for job_id in job_ids:
        job = session.get(models.Job, job_id)
        if job is not None:
            session.delete(job)
    session.commit()
    session.close()


def test_reap_stale_jobs_reclaims_old_processing_and_leaves_recent_alone():
    from app.db import models
    from app.db.base import SessionLocal
    from app.worker.claim import reap_stale_jobs

    session = SessionLocal()
    old_job_id = _make_queued_job(session, models)
    recent_job_id = _make_queued_job(session, models)

    old_job = session.get(models.Job, old_job_id)
    old_job.status = models.JobStatusEnum.processing
    old_job.lease_token = str(uuid.uuid4())
    session.commit()
    # Explicit assignment wins over the column's onupdate=_now default for this flush.
    old_job.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()

    recent_job = session.get(models.Job, recent_job_id)
    recent_job.status = models.JobStatusEnum.processing
    recent_job.lease_token = str(uuid.uuid4())
    session.commit()
    session.close()

    session = SessionLocal()
    reclaimed_count = reap_stale_jobs(session)
    session.close()

    assert reclaimed_count >= 1

    session = SessionLocal()
    old_job = session.get(models.Job, old_job_id)
    recent_job = session.get(models.Job, recent_job_id)

    assert old_job.status == models.JobStatusEnum.queued
    assert old_job.lease_token is None
    assert recent_job.status == models.JobStatusEnum.processing  # untouched - not stale yet

    session.delete(old_job)
    session.delete(recent_job)
    session.commit()
    session.close()
