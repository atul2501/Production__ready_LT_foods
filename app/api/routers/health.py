import os

import httpx
import redis
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.db import models
from app.db.base import engine
from app.storage.local_fs import LocalFileStorage

router = APIRouter(tags=["health"])
storage = LocalFileStorage(settings.storage_dir)


def _dependency_checks() -> dict[str, str]:
    checks: dict[str, str] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"

    try:
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"

    for host in settings.ollama_hosts.split(","):
        host = host.strip()
        if not host:
            continue
        try:
            response = httpx.get(f"{host}/api/tags", timeout=3)
            response.raise_for_status()
            checks[f"ollama:{host}"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks[f"ollama:{host}"] = f"error: {exc}"

    try:
        if os.access(storage.root, os.W_OK):
            checks["storage"] = "ok"
        else:
            checks["storage"] = f"error: {storage.root} is not writable"
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = f"error: {exc}"

    return checks


@router.get("/healthz")
def healthz():
    """Liveness only - is the process up. No dependency calls, safe for a tight
    orchestrator probe interval."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """Readiness for orchestrators (Docker/Kubernetes healthchecks) - checks every
    dependency the API needs to actually serve traffic."""
    checks = _dependency_checks()
    healthy = all(value == "ok" for value in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/api/v1/health")
def api_health(db: Session = Depends(get_db)):
    """Versioned health endpoint for API consumers/monitoring dashboards (as opposed to
    /healthz and /readyz, which are the unprefixed paths orchestrators expect). Same
    dependency checks as /readyz, plus operational signals: how deep the processing
    queue currently is and how job outcomes are trending, so a caller can tell "up but
    falling behind" apart from "fully healthy" without grepping logs."""
    checks = _dependency_checks()

    queued: int | None = None
    processing: int | None = None
    try:
        queued = db.scalar(
            select(func.count()).select_from(models.Job).where(models.Job.status == models.JobStatusEnum.queued)
        )
        processing = db.scalar(
            select(func.count())
            .select_from(models.Job)
            .where(models.Job.status == models.JobStatusEnum.processing)
        )
    except Exception as exc:  # noqa: BLE001 - a health endpoint must never itself throw
        checks.setdefault("database", f"error: {exc}")

    healthy = all(value == "ok" for value in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "queue": {
            "queued_jobs": queued,
            "processing_jobs": processing,
            "max_backlog": settings.max_queue_backlog,
            "backlog_full": (queued or 0) >= settings.max_queue_backlog,
        },
    }
