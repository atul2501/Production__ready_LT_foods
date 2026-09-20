# LT Foods Invoice Extraction API

Uploads a scanned/digital invoice PDF and returns structured JSON matching the exact SAP
posting schema (`invoice_header` / `line_items` / `additional_fields`). SAP decides
MIRO vs FB60 by checking whether `po_number` is `null` — this service's only job is to
populate every field correctly, and to say clearly when it isn't confident.

Runs natively on a single server — no Docker, no broker. The API and worker are plain
Python processes (managed by systemd in production); the job queue lives directly in
Postgres.

## Architecture

```
Postman/client --POST binary PDF--> FastAPI (api, uvicorn)
   api: validates PDF, stores file, inserts job row (status=queued), returns 202
        {job_id} immediately (never blocks on extraction) - no dispatch call, the
        worker discovers the row itself by polling

worker_main.py (a thread pool within one process, app/worker/)
   each thread loops: claim one queued+due job (Postgres `SELECT ... FOR UPDATE
   SKIP LOCKED`, race-safe with no external broker), then:
     1. triage        - digital text-layer PDF vs scanned/image PDF (per page)
     2. extract/OCR    - PyMuPDF (digital, inline) / PaddleOCR (scanned, submitted to
                          a shared OCR_PAGE_WORKERS-sized pool so every scanned page
                          in a document OCRs concurrently) -> source_text
     3. LLM structure  - Ollama (gemma4:31b by default) turns source_text into the
                          exact JSON schema (JSON-schema-constrained, temperature=0)
     4. grounding      - every field the LLM output is re-verified against source_text
     5. business rules - mandatory fields, subtotal+tax=total, line_items non-empty
     6. status assign  - success / needs_review / failed + flags[]
     7. persist        - Postgres (JSONB extraction + audit trail), fenced by a
                          lease_token so a reclaimed/duplicate attempt's write is
                          discarded instead of racing the real result

   a reaper thread reclaims jobs stuck in "processing" (worker crash/restart) back
   to "queued" after a stale timeout - the lease_token fencing above is what makes
   this safe even though the timeout itself is deliberately conservative

Client polls GET /jobs/{id} -> GET /jobs/{id}/result, or fetches the original PDF
back via GET /jobs/{id}/pdf.
```

**Why this design, in one sentence:** local open-source LLMs are not reliable enough to
trust blindly on financial data, so the LLM here only *structures* text that deterministic
OCR already extracted, and every value it outputs is checked against that source text
afterward — anything unverifiable is flagged `needs_review`, never silently returned as fact.

**Why Postgres instead of a broker (Redis/Celery):** this runs on one server, not a
distributed cluster, and the `jobs` table is already the durable source of truth — running
a second stateful service just to hand off "a row is ready" duplicates what Postgres
already does natively via `SKIP LOCKED`, at the cost of an entire extra class of failure
modes (broker visibility timeouts, duplicate redelivery) that a DB-native queue doesn't have.

**Why a shared bounded pool for OCR, not one thread per page:** a page's OCR call needs its
own `PaddleOCR` instance (a shared instance across concurrent calls was traced to a real
transient crash — see [Key risks](#key-risks-read-before-treating-this-as-fully-production-ready)),
and each loaded instance has a real memory cost. `OCR_PAGE_WORKERS` (`app/pipeline/run.py`)
is one pool shared by every job the worker processes, not a new pool per job — so the number
of PaddleOCR instances loaded process-wide stays fixed at `OCR_PAGE_WORKERS` regardless of
`WORKER_CONCURRENCY` or how many pages are in flight, while still letting every scanned page
in one document run concurrently instead of one at a time.

## Running it locally

Requires Python 3.11+ and a local Postgres (`brew install postgresql@16` on macOS,
`apt install postgresql` on Linux).

```bash
createuser invoice --pwprompt   # or: psql -c "CREATE USER invoice WITH PASSWORD '...'"
createdb invoices --owner=invoice

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env   # fill in DATABASE_URL/POSTGRES_PASSWORD (matching above) and OLLAMA_API_KEY
alembic upgrade head

uvicorn main:app --reload           # terminal 1
python worker_main.py               # terminal 2
```

Uses [Ollama Cloud](#extraction-engine-ollama-cloud) by default, so no local model pull is
needed. `WORKER_CONCURRENCY` in `.env` controls how many PDFs the worker processes in
parallel (a thread pool within the one process — see [Resilience](#resilience) for
production redundancy via systemd, not extra replicas).

## Using it from Postman

- **POST** `http://localhost:8000/api/v1/invoices`, body = `form-data`, key `file` (type
  File), value = your invoice PDF. Returns `202 { job_id, status: "queued" }` immediately.
  Raw binary body (Postman's "binary" mode, `Content-Type: application/pdf`) also works.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}` — poll until `status` is no longer
  `queued`/`processing`.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}/result` — the extracted JSON,
  wrapped in `{job_id, status, invoice_header, line_items, additional_fields, metadata}`.
  `metadata.flags` lists exactly which fields need a human look and why.
- **GET** `http://localhost:8000/api/v1/jobs/{job_id}/pdf` — streams the original PDF back.
- **GET** `http://localhost:8000/api/v1/jobs?status=needs_review` — review queue.
- **GET** `http://localhost:8000/api/v1/health` — versioned health check: database/ollama/storage
  status plus current queue depth (`queued_jobs`, `processing_jobs`, `backlog_full`). Use this one for
  monitoring dashboards/Postman; `/healthz` and `/readyz` (unprefixed) are for process-manager probes.
- **GET** `http://localhost:8000/metrics` — Prometheus scrape endpoint: queue depth gauges,
  job-outcome counters (`invoice_jobs_completed_total{status=...}`), pipeline duration histogram,
  and Ollama request/token counters (a spend proxy). See [Monitoring](#monitoring) below.

## Extraction engine: Ollama Cloud (gemma4:31b)

The current model is **`gemma4:31b`** (`OLLAMA_MODEL` in `.env`), run on
[Ollama Cloud](https://ollama.com) by default — faster and more accurate than self-hosting on
typical hardware, and it's the only place the model name lives (see `metadata.model_name` on
any result). This is paid (GPU-time billed) and your invoice text leaves your infrastructure —
a deliberate trade-off, not an oversight. The model has changed before this project went
through `qwen2.5:7b-instruct` (self-hosted) then `deepseek-v4.1-flash` (Ollama Cloud) before
landing on the current `gemma4:31b` — each swap was a one-line `.env` change with no code
changes, which is the point of keeping the model name in exactly one setting.

**No cloud provider — Ollama included — guarantees a given model name stays available forever.**
Model catalogs get rotated as better models ship; you'll get a retirement notice like the ones
that prompted the earlier model swaps above, typically with days-to-weeks of notice. Two things
reduce (not eliminate) the risk:
- `OLLAMA_MODEL` is the only place the model name lives — swapping it is a one-line `.env` change plus
  a worker restart (`systemctl restart invoice-worker` in production), no code changes.
- Prefer rolling/family tags with no date suffix (e.g. `gemma4:31b`) over dated snapshots
  (e.g. `gemma4:31b-0731`) — dated snapshots are retired first.

If a retirement notice ever needs an immediate response and you can't switch cloud models fast enough,
self-hosting (`OLLAMA_HOSTS=http://localhost:11434`, no `OLLAMA_API_KEY`, running the native `ollama
serve` binary) is the fallback with zero vendor-retirement exposure — see the commented block in
`.env.example`.

No code or architecture change needed — [OllamaClient](app/services/ollama_client.py) sends the same
request either way, just with an `Authorization: Bearer` header attached when `OLLAMA_API_KEY` is set.

## Capacity planning: handling 200,000 PDFs/month

200,000 PDFs/month averages to **~0.077 jobs/sec** (200,000 ÷ 30 days ÷ 86,400s), i.e. one
job roughly every 13 seconds around the clock — a low bar in absolute terms, but real traffic
is bursty, not evenly spread, so the actual constraint is peak concurrent throughput, not the
monthly average:

- **Per-job time is dominated by the Ollama call**, not OCR or the rest of the pipeline (every
  stage logs its own `duration_ms` — see [Monitoring](#monitoring) — so this is measurable
  per job, not a guess). A digital-text invoice skips OCR entirely; a scanned one now OCRs all
  its pages concurrently (`OCR_PAGE_WORKERS`, see [Architecture](#architecture)) instead of
  page-by-page, so OCR's contribution to per-job time no longer scales with page count the way
  it used to.
- **Sustained throughput ≈ `WORKER_CONCURRENCY ÷ average job time`.** At `WORKER_CONCURRENCY=3`
  (the `.env.example` default) and a job time in the tens of seconds, this comfortably clears
  0.077 jobs/sec with headroom for bursts. Raising `WORKER_CONCURRENCY` scales this linearly —
  as long as the next constraint below allows it.
- **The real ceiling is your Ollama Cloud plan's concurrent-request limit**, not CPU/OCR — see
  the tiers noted in `.env.example` (Free: low, Pro: ~3, Max/Team: ~10). Setting
  `WORKER_CONCURRENCY` above your plan's concurrent-request limit doesn't add throughput; the
  excess requests just queue on Ollama's side. Watch `invoice_ollama_requests_total` and
  `invoice_ollama_eval_tokens_total` (Prometheus) to see actual concurrent usage and spend.
- **Self-hosted Ollama does not scale the same way**: a single local GPU serializes inference,
  so `WORKER_CONCURRENCY` should stay at `1` when self-hosting. At `1` concurrent job, clearing
  0.077 jobs/sec needs an average job time under ~13s — hardware-dependent, and not something
  to assume without measuring on the actual box. This is why Ollama Cloud (or a multi-GPU
  self-hosted setup, outside what this codebase manages) is the practical choice at this volume.
- **Postgres and PaddleOCR are not the bottleneck at this volume**: job claiming is a single
  indexed `SELECT ... FOR UPDATE SKIP LOCKED` (see [Database](#database)), and
  `OCR_PAGE_WORKERS` bounds OCR to a fixed, CPU-bound cost per page independent of queue depth.
  Neither needs to scale for 200k/month; the Ollama concurrent-request limit does.

In short: to actually sustain 200k/month, size `WORKER_CONCURRENCY` to your Ollama Cloud plan's
concurrent-request limit (raise the plan tier if needed), and use the per-stage timing logs /
`invoice_pipeline_duration_seconds` histogram to confirm real per-job time rather than assuming it.

## Production deployment (systemd)

`deploy/systemd/` has unit files for all four long-running/scheduled pieces:

| Unit | Type | Purpose |
|---|---|---|
| `invoice-api.service` | simple | `uvicorn main:app` |
| `invoice-worker.service` | simple | `worker_main.py` — the polling worker pool |
| `invoice-cleanup.service` + `.timer` | oneshot + daily timer | retention cleanup (below) |
| `invoice-pg-backup.service` + `.timer` | oneshot + daily timer | `scripts/pg_backup.sh` |

Install: copy the app to `/opt/invoice-service`, create a venv there, adjust the `User`/
paths in each unit if needed, then:
```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now invoice-api invoice-worker invoice-cleanup.timer invoice-pg-backup.timer
```

`invoice-api.service` runs Uvicorn with `--workers 4` (tune to actual CPU core count) rather
than a single process — the API route itself is stateless (job handoff is a DB insert the
worker pool polls for, not in-memory state), and blocking work in the upload path (hashing,
disk write, DB calls) already runs via `run_in_threadpool` so it doesn't stall the event
loop either way.

## Database

Postgres is both the system of record and the job queue — no broker, no cache layer.

- **Queue mechanics**: `claim_next_job` (`app/worker/claim.py`) is a single statement —
  `UPDATE jobs SET status='processing' ... WHERE id = (SELECT id FROM jobs WHERE
  status='queued' ... FOR UPDATE SKIP LOCKED LIMIT 1)` — so any number of worker threads can
  claim concurrently with zero coordination code; Postgres's own row-lock manager arbitrates
  it, and `SKIP LOCKED` means a busy claimant never blocks another one, it just takes a
  different row. Indexed on `status` (`Job.status`) and `file_hash` (duplicate-upload
  detection), so claiming and lookups stay O(log n) as the table grows.
- **Connection pooling**: each process (API or worker) opens its own pool —
  `pool_size=10` + `max_overflow=20`, `pool_pre_ping=True` (`app/db/base.py`) — so a stale
  connection is detected and replaced before use rather than surfacing as a query failure.
  With `invoice-api.service` running `--workers 4`, the API alone can open up to
  `4 × (10+20) = 120` connections; check Postgres `max_connections` before raising `--workers`
  or the worker's pool further, and lower `pool_size`/`max_overflow` together with `--workers`
  if you do.
- **Retention**: `app/worker/retention.py` (daily via `invoice-cleanup.timer`) deletes
  completed jobs (`success`/`needs_review`/`failed`) and their stored PDFs once
  `RETENTION_DAYS` (default **1**, i.e. 24 hours) old — batched
  (`RETENTION_BATCH_SIZE`, default 500), file-deleted-before-row so a crash mid-run just
  retries the same rows on the next run instead of leaking orphaned files. `queued`/
  `processing` jobs are never touched regardless of age. A 24-hour default is deliberately
  short — raise `RETENTION_DAYS` in `.env` if the review queue needs a longer window to look
  back at completed extractions.
- **Backups**: see [Resilience](#resilience) below for the daily/weekly/monthly schedule and
  restore command.

## Logging

- **stdout**: structured JSON (via `structlog`, `app/logging_conf.py`) — every pipeline stage
  logs its own start/complete event with a `duration_ms`, correlated by `job_id`, so a slow or
  wrong extraction can be traced back to the exact stage that caused it without extra
  instrumentation (see [Capacity planning](#capacity-planning-handling-200000-pdfsmonth)).
  Under systemd this goes to `journalctl`, which has its own independent retention
  (`journalctl --vacuum-time`) — nothing extra to manage there.
- **`LOG_FILE`** (optional, off by default): when set, the same JSON lines are also appended
  to a file on disk. This file has **no built-in size/age cap** — `RETENTION_DAYS` only
  governs DB job rows + stored PDFs (see [Database](#database) above), never this file.
  `deploy/logrotate/invoice-service` rotates it daily via the OS's standard `logrotate`
  (already run from `cron.daily` on any normal distro — no separate timer needed):
  ```bash
  sudo cp deploy/logrotate/invoice-service /etc/logrotate.d/invoice-service
  ```
  Update the path inside that file first if `LOG_FILE` isn't the default
  `/opt/invoice-service/logs/app.log`.

## Monitoring

`GET /metrics` (Prometheus text format, see `app/metrics.py` + `app/api/routers/metrics.py`)
exposes:
- `invoice_jobs_queued` / `invoice_jobs_processing` — current queue depth (same numbers as
  `/api/v1/health`, refreshed on every scrape).
- `invoice_jobs_completed_total{status="success"|"needs_review"|"failed"}` — job outcomes;
  alert on a rising `needs_review` or `failed` share of the total.
- `invoice_pipeline_duration_seconds` — end-to-end pipeline duration histogram.
- `invoice_ollama_requests_total{outcome=...}` and `invoice_ollama_eval_tokens_total` — a
  spend proxy for Ollama Cloud usage; alert on a sustained jump in either.

This gets metrics into a scrapeable form but doesn't stand up Prometheus/Alertmanager or
wire actual alert rules/notification channels — that still needs to be pointed at your own
monitoring stack.

## Resilience

- **Worker crash recovery**: a reaper thread reclaims jobs stuck in `processing` (crash,
  `systemctl restart`, OOM) back to `queued` after `worker_stale_threshold_seconds` (default
  20 min — deliberately conservative). Correctness against a slow-but-still-alive job
  finishing *after* being reclaimed comes from `lease_token` fencing on the final write
  (see [app/worker/processing.py](app/worker/processing.py)), not from the timeout being
  exactly right — verified directly: killed the worker mid-job, confirmed the job was
  reclaimed and completed correctly on restart with no duplicate/corrupted result.
- **Automatic retry with backoff**: unexpected errors (not a clean extraction failure) get
  requeued with exponential backoff (`next_attempt_at`, 30s→60s→120s, capped, 3 attempts)
  before being marked `failed` — no broker needed, Postgres is the timer.
- **Postgres backups**: `scripts/pg_backup.sh` (daily via `invoice-pg-backup.timer`) — 7
  daily / 4 weekly / 6 monthly kept in `backups/postgres/`. **Restore** with:
  ```bash
  gunzip -c backups/postgres/daily/invoices-<date>.sql.gz | psql -U invoice -d invoices
  ```
  Test this occasionally — an unverified backup isn't a real backup.
- **Retention cleanup**: see [Database](#database) above (`RETENTION_DAYS`, default 1 day).
- **Still a single machine**: one Postgres instance, no replication/failover — backups
  reduce data-loss risk, but a host failure still means downtime until manually recovered.
  Managed Postgres (RDS-style) or real clustering is the next step before multi-machine/HA.

## Key risks (read before treating this as fully production-ready)

What's still genuinely open:

- **No API authentication, no rate limiting.** Anyone who can reach the API can upload
  PDFs and read every job's data, including bank details and VAT numbers — no access
  control, no per-caller audit trail. Add before exposing this beyond localhost.
- **Secrets in a plaintext `.env`.** `OLLAMA_API_KEY`/`POSTGRES_PASSWORD` are fine for
  local dev but belong in a real secrets manager (Vault, AWS Secrets Manager, systemd
  credentials) before production.
- **No TLS.** The API serves plain HTTP — put it behind a reverse proxy (nginx/Caddy) with
  a real certificate before exposing it beyond localhost.
- **Metrics are exposed but not alerted on.** `GET /metrics` (see
  [Monitoring](#monitoring)) gives Prometheus-scrapeable job-outcome, queue-depth, and
  Ollama-usage series, but nothing currently scrapes it or fires an alert — a spike in
  `needs_review` rate, a sustained failure rate, or an Ollama Cloud cost blowout would still
  go unnoticed until someone checks manually or points a monitoring stack at the endpoint.
- **`company_code`/`currency`** are inference-heavy (nothing to ground against) and are
  always flagged for review by design. There is no vendor-hints lookup wired in today, so
  every job needs a human to confirm these two fields.
- A transient PaddleOCR crash was observed once on a real scanned invoice (self-healed on
  retry), suspected to be a thread-safety interaction from sharing one `PaddleOCR` instance
  across concurrent OCR calls. Fixed by giving each concurrently-active thread its own OCR
  engine instance instead of a shared global one — now scoped to the shared `OCR_PAGE_WORKERS`
  pool (`app/pipeline/run.py`) that OCRs every scanned page of a document concurrently rather
  than one at a time, so the same isolation principle now covers page-level parallelism too,
  not just cross-job parallelism. `OCR_TIMEOUT_SECONDS` gives each page a wall-clock ceiling
  so a hung/pathological page fails cleanly instead of tying up a thread indefinitely — though
  since the pool's internal queue is unbounded, that timeout is measured from submission, so a
  heavy burst can trip it on queue wait rather than a genuinely hung page. Accepted tradeoff:
  a tripped timeout just fails that job cleanly and retries via `WORKER_MAX_RETRIES`/backoff.
  Not yet re-verified under sustained load at a raised `WORKER_CONCURRENCY`.
- **No API authentication, no rate limiting, secrets in plaintext `.env`, no TLS** — still
  fully open. Required before exposing this beyond localhost.
- One Postgres instance, no replication — see [Resilience](#resilience) above. Moving to a
  managed/replicated Postgres (e.g. RDS-style) is an infrastructure decision outside what a
  code change alone can provide.
- Steady-state throughput is bounded by `WORKER_CONCURRENCY` and your Ollama Cloud plan's
  concurrent-request limit — raise `WORKER_CONCURRENCY` in step with a higher-tier plan, or
  evaluate self-hosted GPU inference, and watch `invoice_ollama_requests_total` /
  `invoice_ollama_eval_tokens_total` (see [Monitoring](#monitoring)) once you do.
