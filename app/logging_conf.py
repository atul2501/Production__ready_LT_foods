import logging
import sys
from pathlib import Path

import structlog

from app.config import settings


def configure_logging() -> None:
    """Logs JSON lines to stdout (journalctl under systemd) and, when LOG_FILE is set, also
    to a file on disk - every line already carries an ISO timestamp (structlog's TimeStamper)
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
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str = "invoice_service"):
    return structlog.get_logger(name)
