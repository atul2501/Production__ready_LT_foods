from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_hosts: str = "http://localhost:11434"
    ollama_model: str = "gemma4:31b"
    ollama_num_ctx: int = 16384
    ollama_timeout_seconds: int = 120
    # Set to use Ollama Cloud (https://ollama.com) instead of / alongside a self-hosted
    # instance - sent as "Authorization: Bearer <key>" on every request. Leave unset for a
    # purely self-hosted setup. Applies to every host in ollama_hosts, so don't mix a local
    # and a cloud host in the same list if only one of them needs the key.
    ollama_api_key: str | None = None

    storage_dir: str = "/var/lib/invoice-service/pdfs"

    # Required in the X-API-Key header on /api/v1/invoices*. Unset = those endpoints refuse
    # every request (503), so the API can't accidentally run open.
    api_key: str | None = None

    grounding_fuzzy_threshold: float = 90.0
    arithmetic_tolerance_abs: float = 0.02
    arithmetic_tolerance_rel: float = 0.005

    ocr_min_confidence: float = 0.80
    digital_text_min_chars_per_page: int = 200
    # A pathological scanned page (huge/corrupt image) has no other bound on how long OCR
    # can run - this is what turns that into a clean OcrTimeoutError (retried next poll)
    # instead of the email poller hanging on one page forever.
    ocr_timeout_seconds: int = 90
    # Bounds how many PaddleOCR engine instances are loaded process-wide: all PDFs' scanned
    # pages are OCR'd through one shared pool of this size, not a new pool per PDF.
    # CPU-only PaddleOCR (see requirements.txt) - tune to the box's core count.
    ocr_page_workers: int = 4

    # Email ingestion (IMAP) - app/email_ingest/, run as its own service (email_ingest_main.py).
    # Every PDF attached to an unread message is extracted to STORAGE_DIR/pending/ and
    # handed out by GET /api/v1/invoices/new; imap_username/password is a mailbox login (an
    # app password for providers that require one, e.g. Gmail/Yahoo with 2FA).
    imap_host: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    # Comma-separated senders whose PDFs are extracted - full addresses (ap@chep.com) or
    # whole domains (@chep.com), case-insensitive. Mail from anyone else is left unread and
    # ignored. Empty = accept every sender (logged as a warning at startup).
    imap_allowed_senders: str = ""
    # How many polls a PDF that fails extraction is retried on (the email stays unread
    # meanwhile) before a "failed" result is handed out and the email is marked read.
    email_max_attempts: int = 3
    # If set, a fully-ingested message is moved here instead of just being flagged Seen -
    # gives an audit trail of what the ingester actually consumed. Leave unset to just mark
    # Seen and leave the message where it is.
    imap_processed_folder: str | None = None
    imap_poll_interval_seconds: float = 60.0
    imap_timeout_seconds: float = 30.0

    log_level: str = "INFO"
    # When set, JSON logs are also written to this file path (in addition to stdout) - a
    # real filesystem path now that everything runs natively, no volume indirection needed.
    log_file: str | None = None


settings = Settings()
