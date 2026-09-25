import logging
import sys
import time
from datetime import date
from pathlib import Path

import structlog

from app.config import settings


class DailyFileHandler(logging.Handler):
    """Appends to one file per day - LOG_FILE "logs/app.log" becomes "logs/app-2026-09-25.log"
    - and deletes files older than log_retention_days whenever the day changes. Works on
    Windows too (no renaming of open files, unlike logging's rotating handlers), and the
    API and email poller processes can safely both append to the same day's file."""

    def __init__(self, base_path: Path, retention_days: int):
        super().__init__()
        self._base = base_path
        self._retention_days = retention_days
        self._day: date | None = None
        self._stream = None

    def _path_for(self, day: date) -> Path:
        return self._base.with_name(f"{self._base.stem}-{day.isoformat()}{self._base.suffix}")

    def _roll_if_new_day(self) -> None:
        today = date.today()
        if today == self._day:
            return
        if self._stream is not None:
            self._stream.close()
        self._base.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(self._path_for(today), "a", encoding="utf-8")
        self._day = today
        self._delete_old_files()

    def _delete_old_files(self) -> None:
        cutoff = time.time() - self._retention_days * 86400
        for old in self._base.parent.glob(f"{self._base.stem}-*{self._base.suffix}"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass  # already deleted by the other process, or still open - next day retries

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.lock:
                self._roll_if_new_day()
                self._stream.write(self.format(record) + "\n")
                self._stream.flush()
        except Exception:  # noqa: BLE001 - logging must never crash the app
            self.handleError(record)

    def close(self) -> None:
        with self.lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        super().close()


def configure_logging() -> None:
    """Logs JSON lines to stdout (journalctl under systemd) and, when LOG_FILE is set, also
    to a daily file on disk (see DailyFileHandler) - every line already carries an ISO timestamp (structlog's TimeStamper)
    and every pipeline/task stage logs its own start/complete event, so the file is a full
    start-to-end, timestamped trace per job without any extra work at the call site.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if settings.log_file:
        file_handler = DailyFileHandler(Path(settings.log_file), settings.log_retention_days)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str = "invoice_service"):
    return structlog.get_logger(name)
