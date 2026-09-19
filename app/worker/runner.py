import signal
import threading
from types import FrameType

from app.config import settings
from app.db.base import SessionLocal
from app.logging_conf import configure_logging, get_logger
from app.worker.claim import claim_next_job, reap_stale_jobs
from app.worker.processing import process_job

logger = get_logger(__name__)

_shutdown_event = threading.Event()


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    logger.info("shutdown_signal_received", signum=signum)
    _shutdown_event.set()


def _worker_loop(worker_index: int) -> None:
    log = logger.bind(worker_thread=worker_index)
    log.info("worker_thread_started")
    while not _shutdown_event.is_set():
        session = SessionLocal()
        try:
            claimed = claim_next_job(session)
        except Exception:  # noqa: BLE001
            log.exception("claim_failed")
            session.rollback()
            claimed = None
        finally:
            session.close()

        if claimed is None:
            _shutdown_event.wait(settings.worker_poll_interval_seconds)
            continue

        job_id, lease_token = claimed
        session = SessionLocal()
        try:
            process_job(session, job_id, lease_token)
        except Exception:  # noqa: BLE001 - process_job handles its own errors; this is a last resort
            log.exception("process_job_crashed", job_id=job_id)
        finally:
            session.close()

    log.info("worker_thread_stopped")


def _reaper_loop() -> None:
    log = logger.bind(worker_thread="reaper")
    log.info("reaper_started")
    while not _shutdown_event.is_set():
        session = SessionLocal()
        try:
            reap_stale_jobs(session)
        except Exception:  # noqa: BLE001
            log.exception("reap_failed")
            session.rollback()
        finally:
            session.close()
        _shutdown_event.wait(settings.worker_reap_interval_seconds)
    log.info("reaper_stopped")


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info("worker_starting", concurrency=settings.worker_concurrency)

    threads = [
        threading.Thread(target=_worker_loop, args=(i,), name=f"worker-{i}", daemon=False)
        for i in range(settings.worker_concurrency)
    ]
    threads.append(threading.Thread(target=_reaper_loop, name="reaper", daemon=False))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info("worker_stopped")


if __name__ == "__main__":
    main()
