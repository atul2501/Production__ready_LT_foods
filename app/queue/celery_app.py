from celery import Celery
from celery.signals import setup_logging

from app.config import settings
from app.logging_conf import configure_logging

celery_app = Celery("invoice_extraction", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    # Each concurrent slot (thread/process) should only pull one task at a time rather than
    # buffering extras - avoids one slot hoarding several queued jobs while others idle.
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_default_retry_delay=30,
    # Redis broker defaults to a 1-hour visibility timeout: if a worker dies/restarts while
    # holding an unacked task (acks_late=True means it isn't acked until the task finishes),
    # that task is orphaned - invisible to every other worker - for up to an hour before
    # Celery reclaims and redelivers it. Observed directly: two worker restarts during a
    # deploy orphaned in-flight jobs for 20+ minutes with zero further log activity. Set
    # comfortably above the worst realistic single-task duration (LLM timeout retries can
    # legitimately take up to ~12 min) so we don't redeliver a task that's still genuinely
    # running, but far below the 1-hour default.
    broker_transport_options={"visibility_timeout": 1800},
)

celery_app.autodiscover_tasks(["app.queue"])


@setup_logging.connect
def _configure_worker_logging(**kwargs) -> None:
    # Connecting to this signal disables Celery's own logging setup entirely (its documented
    # escape hatch) - without it, Celery overwrites our handlers (file + stdout JSON) right
    # after worker startup and every pipeline log silently stops reaching the log file.
    configure_logging()
