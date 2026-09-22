import signal
import threading
from types import FrameType

from app.config import settings
from app.email_ingest.ingest import run_ingest_cycle
from app.logging_conf import configure_logging, get_logger

logger = get_logger(__name__)

_shutdown_event = threading.Event()


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    logger.info("shutdown_signal_received", signum=signum)
    _shutdown_event.set()


def _ingest_loop() -> None:
    logger.info("email_ingest_loop_started")
    while not _shutdown_event.is_set():
        try:
            run_ingest_cycle()
        except Exception:  # noqa: BLE001 - a bad poll (e.g. IMAP hiccup) must not kill the loop
            logger.exception("email_ingest_cycle_failed")
        _shutdown_event.wait(settings.imap_poll_interval_seconds)
    logger.info("email_ingest_loop_stopped")


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info(
        "email_ingest_starting",
        imap_host=settings.imap_host,
        imap_folder=settings.imap_folder,
        poll_interval_seconds=settings.imap_poll_interval_seconds,
    )
    _ingest_loop()
    logger.info("email_ingest_stopped")


if __name__ == "__main__":
    main()
