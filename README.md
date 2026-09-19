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
     2. extract/OCR    - PyMuPDF (digital) / PaddleOCR (scanned) -> source_text
     3. LLM structure  - Ollama turns source_text into the exact JSON schema
                          (JSON-schema-constrained, temperature=0)
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

Or run `./scripts/smoke_test.sh path/to/sample.pdf` for the same flow via curl.

## Extraction engine: Ollama Cloud

The extraction engine runs on [Ollama Cloud](https://ollama.com) by default — faster and more accurate
than self-hosting on typical hardware (verified: ~20s/PDF vs ~100s/PDF, and it fixed real extraction
errors the self-hosted model made, see `metadata.model_name` on any result). This is paid (GPU-time
billed) and your invoice text leaves your infrastructure — a deliberate trade-off, not an oversight.

**No cloud provider — Ollama included — guarantees a given model name stays available forever.**
Model catalogs get rotated as better models ship; you'll get a retirement notice like the one that
prompted this section, typically with days-to-weeks of notice. Two things reduce (not eliminate) the
risk:
- `OLLAMA_MODEL` is the only place the model name lives — swapping it is a one-line `.env` change plus
  a worker restart (`systemctl restart invoice-worker` in production), no code changes.
- Prefer rolling/family tags with no date suffix (`deepseek-v4.1-flash`) over dated snapshots
  (`deepseek-v4-flash:0731`) — dated snapshots are retired first.

If a retirement notice ever needs an immediate response and you can't switch cloud models fast enough,
self-hosting (`OLLAMA_HOSTS=http://localhost:11434`, no `OLLAMA_API_KEY`, running the native `ollama
serve` binary) is the fallback with zero vendor-retirement exposure — see the commented block in
`.env.example`.

No code or architecture change needed — [OllamaClient](app/services/ollama_client.py) sends the same
request either way, just with an `Authorization: Bearer` header attached when `OLLAMA_API_KEY` is set.

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
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now invoice-api invoice-worker invoice-cleanup.timer invoice-pg-backup.timer
```

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
  Test this occasionally — an unverified backup isn't a real backup. (Verified during
  development: seeded data, backed up, restored into a scratch DB, row counts matched exactly.)
- **Retention cleanup**: `app/worker/retention.py` (daily via `invoice-cleanup.timer`)
  deletes completed jobs (`success`/`needs_review`/`failed`) and their stored PDFs once
  `RETENTION_DAYS` (default 30) old — batched, file-deleted-before-row for self-healing on
  a crash mid-run. `queued`/`processing` jobs are never touched regardless of age.
- **Still a single machine**: one Postgres instance, no replication/failover — backups
  reduce data-loss risk, but a host failure still means downtime until manually recovered.
  Managed Postgres (RDS-style) or real clustering is the next step before multi-machine/HA.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                                        # fast unit tests, no live services needed
RUN_INTEGRATION=1 pytest tests/integration      # requires a live Postgres matching DATABASE_URL
python -m tests.golden.run_golden_set           # once tests/golden/samples + expected/ are populated
```

`tests/integration/test_worker_queue.py` covers the two correctness-critical pieces of the
queue directly against real Postgres (not mockable): concurrent `SKIP LOCKED` claiming
across threads (no duplicates, none skipped) and stale-job reaping.

## Key risks (read before treating this as fully production-ready)

Verified against a real 56-invoice sample across 30 vendor formats (see `logs/` for the
run) — the extraction/grounding/queue architecture holds up, including live-tested worker
crash recovery and backup/restore. What's still genuinely open:

- **No API authentication, no rate limiting.** Anyone who can reach the API can upload
  PDFs and read every job's data, including bank details and VAT numbers — no access
  control, no per-caller audit trail. Add before exposing this beyond localhost.
- **Secrets in a plaintext `.env`.** `OLLAMA_API_KEY`/`POSTGRES_PASSWORD` are fine for
  local dev but belong in a real secrets manager (Vault, AWS Secrets Manager, systemd
  credentials) before production.
- **No TLS.** The API serves plain HTTP — put it behind a reverse proxy (nginx/Caddy) with
  a real certificate before exposing it beyond localhost.
- **No monitoring/alerting** on the structured logs — a spike in `needs_review` rate, a
  sustained failure rate, or an Ollama Cloud cost blowout would go unnoticed until someone
  checks manually.
- **`company_code`/`currency`** are inference-heavy (nothing to ground against) and are
  always flagged for review by design — build out `app/services/vendor_hints.py` with
  validated vendor-specific rules to reduce this over time.
- Golden-set regression testing is wired up but inert until the 306 sample PDFs (and
  their manually-verified expected JSON) are dropped into `tests/golden/samples/` and
  `tests/golden/expected/` — today's confidence comes from manual smoke tests, not an
  automated, repeatable accuracy gate.
- A transient PaddleOCR crash was observed once on a real scanned invoice (self-healed on
  retry) — root cause not fully diagnosed, suspected thread-safety interaction with
  concurrent OCR in the worker's thread pool. Watch for recurrence if scanned-PDF volume grows.
- One Postgres instance, no replication — see [Resilience](#resilience) above.
