from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db import models
from app.metrics import JOBS_PROCESSING, JOBS_QUEUED

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> Response:
    """Prometheus scrape endpoint. Unauthenticated, same as /healthz and /readyz - restrict
    it at the reverse-proxy/firewall level like those, don't rely on obscurity. Queue-depth
    gauges are refreshed from the DB on every scrape (cheap, same query /api/v1/health
    already runs); job-outcome counters and Ollama usage counters live in app/metrics.py
    and are incremented directly where those events happen (app/worker/processing.py,
    app/services/ollama_client.py)."""
    queued = db.scalar(
        select(func.count()).select_from(models.Job).where(models.Job.status == models.JobStatusEnum.queued)
    )
    processing = db.scalar(
        select(func.count())
        .select_from(models.Job)
        .where(models.Job.status == models.JobStatusEnum.processing)
    )
    JOBS_QUEUED.set(queued or 0)
    JOBS_PROCESSING.set(processing or 0)

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
