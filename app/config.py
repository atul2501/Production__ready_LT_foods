from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://invoice:invoice@postgres:5432/invoices"
    redis_url: str = "redis://redis:6379/0"

    ollama_hosts: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_num_ctx: int = 16384
    ollama_timeout_seconds: int = 120
    # Set to use Ollama Cloud (https://ollama.com) instead of / alongside a self-hosted
    # instance - sent as "Authorization: Bearer <key>" on every request. Leave unset for a
    # purely self-hosted setup. Applies to every host in ollama_hosts, so don't mix a local
    # and a cloud host in the same list if only one of them needs the key.
    ollama_api_key: str | None = None

    storage_dir: str = "/data/pdfs"

    max_queue_backlog: int = 5000

    grounding_fuzzy_threshold: float = 90.0
    arithmetic_tolerance_abs: float = 0.02
    arithmetic_tolerance_rel: float = 0.005

    ocr_min_confidence: float = 0.80
    digital_text_min_chars_per_page: int = 200

    log_level: str = "INFO"
    # When set, JSON logs are also written to this file path (in addition to stdout) - see
    # docker-compose.yml, which bind-mounts ./logs on the host to this path in both
    # containers so the file is directly browsable from the project folder.
    log_file: str | None = None


settings = Settings()
