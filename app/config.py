from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://invoice:invoice@localhost:5432/invoices"

    ollama_hosts: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_num_ctx: int = 16384
    ollama_timeout_seconds: int = 120
    # Set to use Ollama Cloud (https://ollama.com) instead of / alongside a self-hosted
    # instance - sent as "Authorization: Bearer <key>" on every request. Leave unset for a
    # purely self-hosted setup. Applies to every host in ollama_hosts, so don't mix a local
    # and a cloud host in the same list if only one of them needs the key.
    ollama_api_key: str | None = None

    storage_dir: str = "/var/lib/invoice-service/pdfs"

    max_queue_backlog: int = 5000

    # Postgres-backed queue (replaces Celery/Redis - see app/worker/)
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 2.0
    # How long a job can sit in "processing" with no result before the reaper assumes the
    # worker that claimed it died and puts it back in the queue. Flat timeout, not a
    # heartbeat - kept comfortably above the worst real job time observed in testing
    # (~6.5 min) with margin. Correctness against a slow-but-still-alive job finishing
    # after being reclaimed comes from lease_token fencing on the final write, not from
    # this number being exactly right.
    worker_stale_threshold_seconds: int = 1200
    worker_reap_interval_seconds: int = 60
    worker_max_retries: int = 3
    worker_retry_base_delay_seconds: int = 30
    worker_retry_max_delay_seconds: int = 300

    # Retention cleanup (run daily, e.g. via a systemd timer - see deploy/systemd/)
    retention_days: int = 30
    retention_batch_size: int = 500

    grounding_fuzzy_threshold: float = 90.0
    arithmetic_tolerance_abs: float = 0.02
    arithmetic_tolerance_rel: float = 0.005

    ocr_min_confidence: float = 0.80
    digital_text_min_chars_per_page: int = 200

    log_level: str = "INFO"
    # When set, JSON logs are also written to this file path (in addition to stdout) - a
    # real filesystem path now that everything runs natively, no volume indirection needed.
    log_file: str | None = None


settings = Settings()
