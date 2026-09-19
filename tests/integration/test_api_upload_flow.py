"""Upload -> insert -> persist -> retrieve round trip against a real Postgres. This only
exercises the API layer (insert + read back) - actually completing a job requires
worker_main.py running alongside the test (it wasn't testing that before the Celery/Redis
removal either, so no coverage regression; see test_worker_queue.py and
test_worker_processing.py for the worker-side logic).

Requires a live Postgres matching DATABASE_URL - skipped otherwise so `pytest` stays fast
and dependency-free by default. Run explicitly with:

    RUN_INTEGRATION=1 pytest tests/integration/test_api_upload_flow.py
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="requires a live Postgres matching DATABASE_URL; set RUN_INTEGRATION=1 to run",
)


def test_upload_flow_returns_202_and_eventually_resolves(tmp_path):
    import fitz
    from fastapi.testclient import TestClient

    from main import app

    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Invoice Number: TEST-1\nTotal: 100.00\n" * 10)
    doc.save(str(pdf_path))
    doc.close()

    client = TestClient(app)
    with open(pdf_path, "rb") as f:
        response = client.post("/api/v1/invoices", files={"file": ("sample.pdf", f, "application/pdf")})

    assert response.status_code == 202
    body = response.json()
    assert "job_id" in body
    assert body["status"] == "queued"

    status_response = client.get(f"/api/v1/jobs/{body['job_id']}")
    assert status_response.status_code == 200

    pdf_response = client.get(f"/api/v1/jobs/{body['job_id']}/pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
