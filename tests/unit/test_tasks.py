from unittest.mock import MagicMock

import pytest

from app.db import models
from app.queue import tasks


class _FakeSession:
    def __init__(self, job):
        self._job = job
        self.committed = False
        self.closed = False

    def get(self, model, job_id):
        return self._job

    def add(self, obj):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _make_job(status: models.JobStatusEnum) -> models.Job:
    return models.Job(
        id="job-1",
        file_hash="hash",
        original_filename="test.pdf",
        storage_key="job-1/original.pdf",
        status=status,
    )


@pytest.mark.parametrize(
    "terminal_status",
    [models.JobStatusEnum.success, models.JobStatusEnum.needs_review, models.JobStatusEnum.failed],
)
def test_duplicate_delivery_of_already_resolved_job_is_skipped(monkeypatch, terminal_status):
    job = _make_job(terminal_status)
    fake_session = _FakeSession(job)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: fake_session)

    run_pipeline_mock = MagicMock()
    monkeypatch.setattr(tasks, "run_pipeline", run_pipeline_mock)
    storage_read_mock = MagicMock()
    monkeypatch.setattr(tasks.storage, "read", storage_read_mock)

    tasks.process_invoice.run("job-1")

    run_pipeline_mock.assert_not_called()
    storage_read_mock.assert_not_called()
    assert job.status == terminal_status  # untouched, not silently overwritten


def test_fresh_queued_job_is_processed_normally(monkeypatch):
    job = _make_job(models.JobStatusEnum.queued)
    fake_session = _FakeSession(job)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: fake_session)

    storage_read_mock = MagicMock(return_value=b"%PDF-fake")
    monkeypatch.setattr(tasks.storage, "read", storage_read_mock)

    run_pipeline_mock = MagicMock(
        return_value={
            "extraction": MagicMock(model_dump=lambda: {}),
            "status": "needs_review",
            "flags": [],
            "model_name": "test-model",
            "prompt_version": "v2",
            "extraction_source": "digital",
            "source_text_hash": "abc",
            "processing_time_ms": 100,
        }
    )
    monkeypatch.setattr(tasks, "run_pipeline", run_pipeline_mock)

    tasks.process_invoice.run("job-1")

    run_pipeline_mock.assert_called_once()
    assert job.status == models.JobStatusEnum.needs_review
