"""Prometheus metrics. Note each process has its own registry and only the API process
serves /metrics, so counters incremented in the email poller process (extraction outcomes,
Ollama usage) are not scraped - only activity inside the API process (POST
/api/v1/invoices) and the pending gauge show up there.
"""
from prometheus_client import Counter, Gauge, Histogram

JOBS_COMPLETED = Counter(
    "invoice_jobs_completed_total",
    "Jobs that reached a terminal status, by status",
    ["status"],
)

RESULTS_PENDING = Gauge("invoice_results_pending", "Extracted results not yet fetched via /api/v1/invoices/new")

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
