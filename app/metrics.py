"""Prometheus metrics shared between the API process and the worker process. Both import
this module, so counters incremented in the worker (job outcomes, Ollama usage) and gauges
set in the API's /metrics handler (queue depth) end up in the same registry that the API's
/metrics endpoint serves - Prometheus only ever scrapes the API process, not the worker
directly, since the worker has no HTTP server of its own.
"""
from prometheus_client import Counter, Gauge, Histogram

JOBS_COMPLETED = Counter(
    "invoice_jobs_completed_total",
    "Jobs that reached a terminal status, by status",
    ["status"],
)

JOBS_QUEUED = Gauge("invoice_jobs_queued", "Jobs currently queued")
JOBS_PROCESSING = Gauge("invoice_jobs_processing", "Jobs currently being processed")

PIPELINE_DURATION_SECONDS = Histogram(
    "invoice_pipeline_duration_seconds",
    "End-to-end pipeline duration for a completed job",
    buckets=(5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600),
)

# Proxy for Ollama Cloud spend, which bills on usage - a sustained jump in request rate or
# tokens/request is the earliest signal of a cost blowout, well before a bill arrives.
OLLAMA_REQUESTS = Counter(
    "invoice_ollama_requests_total",
    "Ollama generate calls, by outcome",
    ["outcome"],
)
OLLAMA_EVAL_TOKENS = Counter(
    "invoice_ollama_eval_tokens_total",
    "Sum of eval_count (output tokens) reported by Ollama across all requests",
)
