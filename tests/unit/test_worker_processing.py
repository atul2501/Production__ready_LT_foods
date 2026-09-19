from unittest.mock import MagicMock

import pytest

from app.core.exceptions import LLMFormatError
from app.db import models
from app.worker import processing


class _FakeResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    """Stands in for a SQLAlchemy Session in process_job's control-flow tests. The fenced
    UPDATE (see processing._fenced_update) is exercised via `execute()`'s configured
    rowcount rather than actually mutating `job` - that SQL-level correctness is covered
    by the real-Postgres integration test (tests/integration/test_worker_queue.py); this
    file tests which branch process_job takes given each outcome.
    """

    def __init__(self, job: models.Job, fenced_rowcount: int = 1):
        self._job = job
        self._fenced_rowcount = fenced_rowcount
        self.committed = False
        self.rolled_back = False
        self.added: list = []

    def get(self, model, job_id):
        return self._job

    def add(self, obj):
        self.added.append(obj)

    def execute(self, stmt, *args, **kwargs):
        return _FakeResult(self._fenced_rowcount)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _make_job(
    status: models.JobStatusEnum = models.JobStatusEnum.processing,
    lease_token: str = "lease-abc",
    retry_count: int = 0,
) -> models.Job:
    return models.Job(
        id="job-1",
        file_hash="hash",
        storage_key="job-1/original.pdf",
        status=status,
        lease_token=lease_token,
        retry_count=retry_count,
    )


def _audit_events(session: _FakeSession, event_type: str) -> list:
    return [o for o in session.added if isinstance(o, models.AuditEvent) and o.event_type == event_type]


def _extraction_rows(session: _FakeSession) -> list:
    return [o for o in session.added if isinstance(o, models.InvoiceExtraction)]


@pytest.mark.parametrize(
    "terminal_status",
    [models.JobStatusEnum.success, models.JobStatusEnum.needs_review, models.JobStatusEnum.failed],
)
def test_duplicate_processing_of_already_resolved_job_is_skipped(monkeypatch, terminal_status):
    job = _make_job(status=terminal_status)
    session = _FakeSession(job)
    run_pipeline_mock = MagicMock()
    monkeypatch.setattr(processing, "run_pipeline", run_pipeline_mock)

    processing.process_job(session, "job-1", "lease-abc")

    run_pipeline_mock.assert_not_called()


def test_extraction_error_marks_job_failed(monkeypatch):
    job = _make_job()
    session = _FakeSession(job, fenced_rowcount=1)
    monkeypatch.setattr(processing, "run_pipeline", MagicMock(side_effect=LLMFormatError("boom")))
    monkeypatch.setattr(processing.storage, "read", MagicMock(return_value=b"%PDF-fake"))

    processing.process_job(session, "job-1", "lease-abc")

    assert len(_audit_events(session, "failed")) == 1


def test_extraction_error_with_stale_lease_is_discarded_not_recorded(monkeypatch):
    job = _make_job()
    session = _FakeSession(job, fenced_rowcount=0)  # simulates: lease no longer matches
    monkeypatch.setattr(processing, "run_pipeline", MagicMock(side_effect=LLMFormatError("boom")))
    monkeypatch.setattr(processing.storage, "read", MagicMock(return_value=b"%PDF-fake"))

    processing.process_job(session, "job-1", "lease-abc")

    assert session.rolled_back is True
    assert len(_audit_events(session, "failed")) == 0


def _fake_pipeline_result() -> dict:
    return {
        "extraction": MagicMock(model_dump=lambda: {}),
        "status": "needs_review",
        "flags": [],
        "model_name": "test-model",
        "prompt_version": "v2",
        "extraction_source": "digital",
        "source_text_hash": "abc",
        "processing_time_ms": 100,
    }


def test_success_result_persists_extraction(monkeypatch):
    job = _make_job()
    session = _FakeSession(job, fenced_rowcount=1)
    monkeypatch.setattr(processing, "run_pipeline", MagicMock(return_value=_fake_pipeline_result()))
    monkeypatch.setattr(processing.storage, "read", MagicMock(return_value=b"%PDF-fake"))

    processing.process_job(session, "job-1", "lease-abc")

    assert len(_extraction_rows(session)) == 1
    assert len(_audit_events(session, "extraction_complete")) == 1


def test_success_result_with_stale_lease_discards_extraction(monkeypatch):
    job = _make_job()
    session = _FakeSession(job, fenced_rowcount=0)
    monkeypatch.setattr(processing, "run_pipeline", MagicMock(return_value=_fake_pipeline_result()))
    monkeypatch.setattr(processing.storage, "read", MagicMock(return_value=b"%PDF-fake"))

    processing.process_job(session, "job-1", "lease-abc")

    assert len(_extraction_rows(session)) == 0
    assert session.rolled_back is True


def test_unexpected_exception_schedules_automatic_retry(monkeypatch):
    job = _make_job(retry_count=0)
    session = _FakeSession(job)
    monkeypatch.setattr(processing.storage, "read", MagicMock(side_effect=RuntimeError("disk exploded")))

    processing.process_job(session, "job-1", "lease-abc")

    assert job.status == models.JobStatusEnum.queued
    assert job.retry_count == 1
    assert job.next_attempt_at is not None
    assert job.lease_token is None
    assert session.committed is True


def test_unexpected_exception_exhausts_retries_marks_failed(monkeypatch):
    job = _make_job(retry_count=3)  # already at worker_max_retries default
    session = _FakeSession(job)
    monkeypatch.setattr(processing.storage, "read", MagicMock(side_effect=RuntimeError("disk exploded")))

    processing.process_job(session, "job-1", "lease-abc")

    assert job.status == models.JobStatusEnum.failed


def test_unexpected_exception_with_stale_lease_is_ignored(monkeypatch):
    job = _make_job(lease_token="a-different-token")
    session = _FakeSession(job)
    monkeypatch.setattr(processing.storage, "read", MagicMock(side_effect=RuntimeError("disk exploded")))

    processing.process_job(session, "job-1", "lease-abc")

    # the stale attempt must never touch a job it no longer holds the lease for
    assert job.status == models.JobStatusEnum.processing
    assert job.retry_count == 0
